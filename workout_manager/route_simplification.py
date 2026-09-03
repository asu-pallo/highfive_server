import math

DEFAULT_TOLERANCE_METERS = 1.0


def simplify_route(points: list[dict], tolerance=DEFAULT_TOLERANCE_METERS) -> list[dict]:
    """지도 표시·다운로드용 경로를 Douglas–Peucker 방식으로 줄인다.

    H3·PR·통계 계산에는 이 결과가 아닌 업로드된 원본 경로를 사용한다.
    """
    if len(points) <= 2:
        return points
    anchor_latitude = sum(float(point['latitude']) for point in points) / len(points)
    projected = [_project(point, anchor_latitude) for point in points]
    keep = {0, len(points) - 1}

    def visit(start, end):
        if end <= start + 1:
            return
        maximum, selected = 0.0, None
        for index in range(start + 1, end):
            distance = _segment_distance(projected[index], projected[start], projected[end])
            if distance > maximum:
                maximum, selected = distance, index
        if selected is not None and maximum > tolerance:
            keep.add(selected)
            visit(start, selected)
            visit(selected, end)

    visit(0, len(points) - 1)
    return [points[index] for index in sorted(keep)]


def _project(point, anchor_latitude):
    latitude = math.radians(float(point['latitude']))
    longitude = math.radians(float(point['longitude']))
    return (
        6_371_008.8 * longitude * math.cos(math.radians(anchor_latitude)),
        6_371_008.8 * latitude,
    )


def _segment_distance(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 and dy == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    ratio = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)))
    nearest = (start[0] + ratio * dx, start[1] + ratio * dy)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])
