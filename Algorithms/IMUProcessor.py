# IMU (MPU6050) motion -> analog stick.
#
# Pipeline per sample (IMU_SAMPLE_RATE, 200 Hz):
#   raw -> units -> gyro bias removed -> Madgwick AHRS -> gravity
#   direction in the sensor frame -> tilt relative to the calibrated neutral pose
#   -> piecewise map through the calibrated L/R/U/D poses -> One Euro smoothing
#   -> radial deadzone -> stick (x, y), each -1..1 (y up = +1).
#
# Using the gravity direction makes the result independent of how the IMU is
# mounted, and drift-free (tilt is referenced to gravity). Yaw (turning about
# the vertical) can't be tracked drift-free with 6 axes, so calibrate with tilts.

import math
import time
from collections import deque

import numpy as np

from Algorithms.OneEuroFilter import OneEuroFilter
from npg_protocol import IMU_SAMPLE_RATE, IMU_ACCEL_LSB_PER_G, IMU_GYRO_LSB_PER_DPS

DEG = math.pi / 180.0

# Tuning
MADGWICK_GAIN = 0.05     # accel correction strength (lower = trusts gyro more)
EURO_MIN_CUTOFF = 3.0    # Hz, smoothing when still
EURO_BETA = 1.0          # cutoff increase with speed (less lag on fast moves)
DEADZONE = 0.05          # radial, fraction of full stick

# Calibration
STEADY_SECONDS = 1.0     # how long a pose must be held
STEADY_DEG = 2.0         # max wobble while holding
STEADY_GYRO_STD = 3.0    # deg/s, max gyro spread while holding (rejects the tail of a movement)
MIN_TILT_DEG = 10.0      # minimum tilt for a direction pose
CENTER_DEG = 6.0         # "back at centre" tolerance between poses
CAL_ROUNDS = 2
CAL_DIRECTIONS = ["LEFT", "RIGHT", "UP", "DOWN"]
# Stick output for each calibrated direction, in circular order
_SECTOR_ORDER = [("RIGHT", (1.0, 0.0)), ("UP", (0.0, 1.0)),
                 ("LEFT", (-1.0, 0.0)), ("DOWN", (0.0, -1.0))]


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _angle_between(a, b):
    return math.atan2(np.linalg.norm(np.cross(a, b)), float(np.dot(a, b)))


def madgwick_imu(q, gx, gy, gz, ax, ay, az, gain, dt):
    """One Madgwick IMU update (gyro rad/s, accel any unit), plain floats.

    Same equations as ahrs.filters.Madgwick.updateIMU (S. Madgwick, 2010,
    eqs. 12, 13, 25, 26, 33, 34) without per-sample numpy overhead.
    q: (w, x, y, z). Returns the new normalised quaternion.
    """
    qw, qx, qy, qz = q
    n = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    qw, qx, qy, qz = qw / n, qx / n, qy / n, qz / n
    if gx == 0.0 and gy == 0.0 and gz == 0.0:
        return (qw, qx, qy, qz)

    # Rate of change from the gyro: 0.5 * q (x) (0, g)                  (eq. 12)
    dw = 0.5 * (-qx * gx - qy * gy - qz * gz)
    dx = 0.5 * (qw * gx + qy * gz - qz * gy)
    dy = 0.5 * (qw * gy - qx * gz + qz * gx)
    dz = 0.5 * (qw * gz + qx * gy - qy * gx)

    a_norm = math.sqrt(ax * ax + ay * ay + az * az)
    if a_norm > 0.0:
        ax, ay, az = ax / a_norm, ay / a_norm, az / a_norm
        # Objective function                                               (eq. 25)
        f0 = 2.0 * (qx * qz - qw * qy) - ax
        f1 = 2.0 * (qw * qx + qy * qz) - ay
        f2 = 2.0 * (0.5 - qx * qx - qy * qy) - az
        if f0 != 0.0 or f1 != 0.0 or f2 != 0.0:
            # Gradient = J^T f                                             (eqs. 26, 34)
            s0 = -2.0 * qy * f0 + 2.0 * qx * f1
            s1 = 2.0 * qz * f0 + 2.0 * qw * f1 - 4.0 * qx * f2
            s2 = -2.0 * qw * f0 + 2.0 * qz * f1 - 4.0 * qy * f2
            s3 = 2.0 * qx * f0 + 2.0 * qy * f1
            sn = math.sqrt(s0 * s0 + s1 * s1 + s2 * s2 + s3 * s3)
            if sn > 0.0:
                dw -= gain * s0 / sn                                     # (eq. 33)
                dx -= gain * s1 / sn
                dy -= gain * s2 / sn
                dz -= gain * s3 / sn

    qw, qx, qy, qz = qw + dw * dt, qx + dx * dt, qy + dy * dt, qz + dz * dt   # (eq. 13)
    n = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    return (qw / n, qx / n, qy / n, qz / n)


def gravity_from_q(q):
    """Gravity direction in the sensor frame for a Madgwick quaternion (w, x, y, z).
    Matches the normalised accelerometer reading at rest."""
    w, x, y, z = q
    return (2 * (x * z - w * y), 2 * (w * x + y * z), w * w - x * x - y * y + z * z)


def q_from_accel(acc):
    """Quaternion whose gravity direction matches a (resting) accel reading, yaw = 0."""
    ax, ay, az = _unit(acc)
    roll = math.atan2(ay, az)
    pitch = math.atan2(-ax, math.hypot(ay, az))
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    return (cr * cp, sr * cp, cr * sp, -sr * sp)


class StickCalibration:
    """Maps a gravity direction onto stick (x, y) using the calibrated poses."""

    def __init__(self, g0, poses):
        """g0: neutral gravity (unit). poses: {'LEFT'|'RIGHT'|'UP'|'DOWN': gravity (unit)}."""
        self.g0 = _unit(np.asarray(g0, dtype=float))
        # Any orthonormal basis of the plane perpendicular to g0
        ref = np.array([1.0, 0.0, 0.0]) if abs(self.g0[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        self.e1 = _unit(np.cross(self.g0, ref))
        self.e2 = np.cross(self.g0, self.e1)

        self.dirs = {k: self.to_plane(v) for k, v in poses.items()}

        # One 2x2 inverse per sector between neighbouring directions
        self._sectors = []
        self._degenerate = False
        for i in range(4):
            (na, oa), (nb, ob) = _SECTOR_ORDER[i], _SECTOR_ORDER[(i + 1) % 4]
            m = np.column_stack([self.dirs[na], self.dirs[nb]])
            try:
                inv = np.linalg.inv(m)
                # Stored as plain floats for the per-sample path
                self._sectors.append((float(inv[0, 0]), float(inv[0, 1]), float(inv[1, 0]), float(inv[1, 1]),
                                      oa, ob))
            except np.linalg.LinAlgError:
                self._degenerate = True     # neighbouring poses parallel; problem() reports it
        self._g0f = tuple(float(v) for v in self.g0)
        self._e1f = tuple(float(v) for v in self.e1)
        self._e2f = tuple(float(v) for v in self.e2)

    def to_plane(self, g):
        """Tilt from neutral as a 2D vector; length = tilt angle in radians."""
        g = _unit(np.asarray(g, dtype=float))
        axis = np.cross(self.g0, g)
        s = np.linalg.norm(axis)
        if s < 1e-9:
            return np.zeros(2)
        angle = math.atan2(s, float(np.dot(self.g0, g)))
        axis = axis / s * angle
        return np.array([np.dot(axis, self.e1), np.dot(axis, self.e2)])

    def map(self, g):
        """Gravity direction (gx, gy, gz) -> stick (x, y). Plain floats (runs per sample)."""
        gx, gy, gz = g
        n = math.sqrt(gx * gx + gy * gy + gz * gz)
        gx, gy, gz = gx / n, gy / n, gz / n
        ax, ay, az = self._g0f
        # Tilt from neutral as a 2D vector (same as to_plane)
        cx, cy, cz = ay * gz - az * gy, az * gx - ax * gz, ax * gy - ay * gx
        s = math.sqrt(cx * cx + cy * cy + cz * cz)
        if s < 1e-9:
            px = py = 0.0
        else:
            k = math.atan2(s, ax * gx + ay * gy + az * gz) / s
            e1, e2 = self._e1f, self._e2f
            px = (cx * e1[0] + cy * e1[1] + cz * e1[2]) * k
            py = (cx * e2[0] + cy * e2[1] + cz * e2[2]) * k

        x = y = 0.0
        for i00, i01, i10, i11, oa, ob in self._sectors:
            s_, t_ = i00 * px + i01 * py, i10 * px + i11 * py
            if s_ >= -1e-9 and t_ >= -1e-9:
                x = s_ * oa[0] + t_ * ob[0]
                y = s_ * oa[1] + t_ * ob[1]
                break
        r = math.hypot(x, y)
        if r > 1.0:
            x, y = x / r, y / r
        return x, y

    def problem(self):
        """Return an error message if the poses can't form a usable map, else None."""
        names = [n for n, _ in _SECTOR_ORDER]
        for n in names:
            if np.linalg.norm(self.dirs[n]) < MIN_TILT_DEG * DEG * 0.8:
                return f"{n.title()} tilt was too small."
        turns = []
        for i in range(4):
            a, b = self.dirs[names[i]], self.dirs[names[(i + 1) % 4]]
            turns.append(math.atan2(a[0] * b[1] - a[1] * b[0], float(np.dot(a, b))))
        same_way = all(t > 0 for t in turns) or all(t < 0 for t in turns)
        if self._degenerate or not same_way or not all(25 * DEG < abs(t) < 155 * DEG for t in turns):
            return "Directions didn't line up (left/right and up/down should be roughly opposite and at right angles)."
        return None


class Calibrator:
    """Guided calibration: per round, hold still (neutral), then hold each of
    LEFT, RIGHT, UP, DOWN, returning to centre in between. Runs CAL_ROUNDS
    rounds and averages them."""

    def __init__(self):
        self.round = 1
        self.step = "NEUTRAL"       # NEUTRAL or one of CAL_DIRECTIONS
        self.phase = "hold"         # 'return' (go back to centre) or 'hold'
        self.progress = 0.0         # 0..1 for the current hold
        self.tilt_deg = 0.0         # current tilt from neutral (degrees)
        self.done = False
        self.error = None
        self.result = None          # StickCalibration when done
        self.gyro_bias = None       # deg/s, from the neutral holds

        self._n = int(STEADY_SECONDS * IMU_SAMPLE_RATE)
        self._win = deque()
        self._neutrals, self._biases = [], []
        self._poses = {d: [] for d in CAL_DIRECTIONS}
        self._g0 = None

    def captured_counts(self):
        """{direction: number of captures so far}."""
        return {d: len(v) for d, v in self._poses.items()}

    def instruction(self):
        if self.error:
            return self.error
        if self.done:
            return "Calibrated. Move the IMU to test the stick."
        prefix = f"Round {self.round} of {CAL_ROUNDS}: "
        if self.step == "NEUTRAL":
            return prefix + "hold still in your centre position"
        if self.phase == "return":
            return prefix + "back to centre"
        return prefix + f"tilt {self.step} and hold"

    def feed(self, acc, gyr_dps):
        """acc: accel in g. gyr_dps: raw gyro in deg/s."""
        if self.done or self.error:
            return
        a = _unit(acc)
        if self._g0 is not None:
            self.tilt_deg = _angle_between(self._g0, a) / DEG

        if self.phase == "return":
            if self.tilt_deg <= CENTER_DEG:
                self.phase = "hold"
                self._win.clear()
            return

        # Steady check: restart the window whenever the pose wobbles
        if self._win:
            mean = _unit(np.mean([w[0] for w in self._win], axis=0))
            if _angle_between(mean, a) > STEADY_DEG * DEG or abs(np.linalg.norm(acc) - 1.0) > 0.15:
                self._win.clear()
        if self.step != "NEUTRAL" and self.tilt_deg < MIN_TILT_DEG:
            self._win.clear()
            self.progress = 0.0
            return
        self._win.append((a, np.asarray(gyr_dps, dtype=float)))
        if len(self._win) >= self._n:
            # Still rotating at the start of the window? Drop the oldest samples and keep going.
            if np.max(np.std([w[1] for w in self._win], axis=0)) > STEADY_GYRO_STD:
                for _ in range(self._n // 10):
                    self._win.popleft()
            else:
                self._capture()
        self.progress = len(self._win) / self._n

    def _capture(self):
        g = _unit(np.mean([w[0] for w in self._win], axis=0))
        gyro_mean = np.median([w[1] for w in self._win], axis=0)
        self._win.clear()
        self.progress = 0.0

        if self.step == "NEUTRAL":
            self._neutrals.append(g)
            self._biases.append(gyro_mean)
            self._g0 = _unit(np.mean(self._neutrals, axis=0))
            self.step, self.phase = CAL_DIRECTIONS[0], "hold"
            return

        self._poses[self.step].append(g)
        i = CAL_DIRECTIONS.index(self.step)
        if i + 1 < len(CAL_DIRECTIONS):
            self.step, self.phase = CAL_DIRECTIONS[i + 1], "return"
        elif self.round < CAL_ROUNDS:
            self.round += 1
            self.step, self.phase = "NEUTRAL", "return"
        else:
            self._finish()

    def _finish(self):
        poses = {d: _unit(np.mean(v, axis=0)) for d, v in self._poses.items()}
        cal = StickCalibration(self._g0, poses)
        err = cal.problem()
        if err:
            self.error = err + " Press RECALIBRATE to try again."
            return
        self.result = cal
        self.gyro_bias = np.mean(self._biases, axis=0)
        self.done = True


class IMUProcessor:
    def __init__(self):
        self.gyro_bias = (0.0, 0.0, 0.0)  # deg/s
        self.calibration = None           # StickCalibration
        self.calibrator = None            # active Calibrator, or None
        self.stick = (0.0, 0.0)           # latest output, -1..1, y up = +1
        self.last_sample_time = 0.0
        self._dt = 1.0 / IMU_SAMPLE_RATE
        self._q = None
        self._fx = OneEuroFilter(IMU_SAMPLE_RATE, EURO_MIN_CUTOFF, EURO_BETA)
        self._fy = OneEuroFilter(IMU_SAMPLE_RATE, EURO_MIN_CUTOFF, EURO_BETA)

    # State

    @property
    def receiving(self):
        return time.monotonic() - self.last_sample_time < 1.0

    @property
    def calibrated(self):
        return self.calibration is not None

    def reset(self):
        """Forget everything (on disconnect)."""
        self.__init__()

    def start_calibration(self, calibrator=None):
        """Begin a guided calibration. Returns the Calibrator so the UI can follow it."""
        self.calibrator = calibrator or Calibrator()
        self._set_stick(0.0, 0.0)
        return self.calibrator

    def cancel_calibration(self):
        self.calibrator = None

    # Processing

    def process(self, samples):
        """samples: list of raw (counter, ax, ay, az, gx, gy, gz)."""
        bx, by, bz = self.gyro_bias
        for _, ax, ay, az, gx, gy, gz in samples:
            if ax == 0 and ay == 0 and az == 0:
                continue
            ax, ay, az = ax / IMU_ACCEL_LSB_PER_G, ay / IMU_ACCEL_LSB_PER_G, az / IMU_ACCEL_LSB_PER_G
            gx, gy, gz = gx / IMU_GYRO_LSB_PER_DPS, gy / IMU_GYRO_LSB_PER_DPS, gz / IMU_GYRO_LSB_PER_DPS

            if self._q is None:
                self._q = q_from_accel((ax, ay, az))
            else:
                self._q = madgwick_imu(self._q, (gx - bx) * DEG, (gy - by) * DEG, (gz - bz) * DEG,
                                       ax, ay, az, MADGWICK_GAIN, self._dt)

            cal = self.calibrator
            if cal is not None:
                acc = np.array((ax, ay, az))
                cal.feed(acc, np.array((gx, gy, gz)))
                if cal.done:
                    self.gyro_bias = tuple(float(v) for v in cal.gyro_bias)
                    bx, by, bz = self.gyro_bias
                    self.calibration = cal.result
                    self.calibrator = None
                    self._q = q_from_accel(acc)
                    self._fx.reset()
                    self._fy.reset()
                continue        # no stick output while calibrating
            g = gravity_from_q(self._q)

            if self.calibration is not None:
                x, y = self.calibration.map(g)
                x, y = self._fx.filter(x), self._fy.filter(y)
                r = math.hypot(x, y)
                if r <= DEADZONE:
                    x = y = 0.0
                else:
                    k = (r - DEADZONE) / (1.0 - DEADZONE) / r
                    x, y = x * k, y * k
                self._set_stick(x, y)
        self.last_sample_time = time.monotonic()

    def _set_stick(self, x, y):
        self.stick = (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))
