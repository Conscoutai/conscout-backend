# Geo utilities: distance and coordinate transforms.
# Used by tours and floorplan mapping.

# utils/geo.py

import math

# ---------------------------------------------------------
# Distance between two GPS points (meters)
# ---------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6378137  # meters
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)

    a = (
        math.sin(dLat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dLon / 2) ** 2
    )

    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------
# Solve linear system (Gaussian elimination)
# ---------------------------------------------------------
def _solve_linear_system(matrix, vector):
    n = len(vector)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]

    for i in range(n):
        pivot = i
        for r in range(i + 1, n):
            if abs(aug[r][i]) > abs(aug[pivot][i]):
                pivot = r
        if abs(aug[pivot][i]) < 1e-12:
            return None
        if pivot != i:
            aug[i], aug[pivot] = aug[pivot], aug[i]

        div = aug[i][i]
        for c in range(i, n + 1):
            aug[i][c] /= div

        for r in range(n):
            if r == i:
                continue
            factor = aug[r][i]
            for c in range(i, n + 1):
                aug[r][c] -= factor * aug[i][c]

    return [aug[i][n] for i in range(n)]


# ---------------------------------------------------------
# Fit affine transform (meters -> pixels) using least squares
# ---------------------------------------------------------
def _fit_affine_from_points(points, origin_lat, origin_lon):
    if len(points) < 3:
        return None

    rows = []
    vals = []
    for point in points:
        pixel = point.get("pixel") or {}
        lat = point.get("latitude")
        lon = point.get("longitude")
        if lat is None or lon is None:
            continue
        if pixel.get("x") is None or pixel.get("y") is None:
            continue

        dx = haversine(origin_lat, origin_lon, origin_lat, lon)
        dy = haversine(origin_lat, origin_lon, lat, origin_lon)
        if lon < origin_lon:
            dx = -dx
        if lat < origin_lat:
            dy = -dy

        rows.append([dx, dy, 1.0, 0.0, 0.0, 0.0])
        rows.append([0.0, 0.0, 0.0, dx, dy, 1.0])
        vals.append(float(pixel["x"]))
        vals.append(float(pixel["y"]))

    if len(rows) < 6:
        return None

    normal = [[0.0] * 6 for _ in range(6)]
    rhs = [0.0] * 6
    for r, row in enumerate(rows):
        for i in range(6):
            rhs[i] += row[i] * vals[r]
            for j in range(6):
                normal[i][j] += row[i] * row[j]

    solution = _solve_linear_system(normal, rhs)
    if not solution:
        return None
    return solution


# ---------------------------------------------------------
# GPS to floorplan pixel mapping
# ---------------------------------------------------------
def gps_to_xy(gps_lat, gps_lon, fp):
    origin_lat = fp["origin"]["latitude"]
    origin_lon = fp["origin"]["longitude"]
    ox = fp["origin"]["pixel"]["x"]
    oy = fp["origin"]["pixel"]["y"]

    calibration_points = fp.get("calibration_points") or []
    affine = _fit_affine_from_points(calibration_points, origin_lat, origin_lon)

    if affine:
        a, b, c, d, e, f = affine
        dx = haversine(origin_lat, origin_lon, origin_lat, gps_lon)
        dy = haversine(origin_lat, origin_lon, gps_lat, origin_lon)
        if gps_lon < origin_lon:
            dx = -dx
        if gps_lat < origin_lat:
            dy = -dy
        px = a * dx + b * dy + c
        py = d * dx + e * dy + f
        return px, py

    scale = fp["scale"]
    rotation = math.radians(fp["rotation"])

    dx = haversine(origin_lat, origin_lon, origin_lat, gps_lon)
    dy = haversine(origin_lat, origin_lon, gps_lat, origin_lon)

    if gps_lon < origin_lon:
        dx = -dx
    if gps_lat < origin_lat:
        dy = -dy

    x_rot = dx * math.cos(rotation) - dy * math.sin(rotation)
    y_rot = dx * math.sin(rotation) + dy * math.cos(rotation)

    px = ox + (x_rot / scale)
    py = oy - (y_rot / scale)

    return px, py


# ---------------------------------------------------------
# Project point using bearing + distance
# ---------------------------------------------------------
def project_point(lat, lon, bearing_deg, distance_m):
    R = 6371000
    bearing = math.radians(bearing_deg)

    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(distance_m / R)
        + math.cos(lat1) * math.sin(distance_m / R) * math.cos(bearing)
    )

    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(distance_m / R) * math.cos(lat1),
        math.cos(distance_m / R) - math.sin(lat1) * math.sin(lat2),
    )

    return math.degrees(lat2), math.degrees(lon2)
