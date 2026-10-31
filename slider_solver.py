"""Optional, local ordinary-gap solver. No network, files, or browser input.

captcha-recognizer 1.0.2 (MIT) ships its ONNX model in the wheel. Imports and
model loading are lazy; missing dependencies/model or uncertain geometry
return not handled. The SMS manager owns all replay, fallbacks and budgets.
"""
import math
import os
import random


def trajectory(start, end, *, rng=None):
    """A small eased path with bounded lateral motion and varied timing.

    Input/output are screenshot-relative coordinates; timestamps are ms. This
    is not a promise of being indistinguishable from human input. Qt's shared
    replayer adds points as needed to keep steps <=8 logical pixels.
    """
    rng = rng or random.SystemRandom()
    if (len(start) != 2 or len(end) != 2
            or any(type(n) not in (int, float) or not math.isfinite(n) or not 0 < n < 1
                   for n in (*start, *end)) or end[0] <= start[0]):
        raise ValueError('invalid_trajectory')
    duration = rng.randint(850, 1350)
    count = rng.randint(32, 44)
    bend = rng.uniform(-.006, .006)
    weights = [rng.uniform(.85, 1.15) for _ in range(count)]
    total, elapsed = sum(weights), 0
    points = [(start[0], start[1], 0)]
    for index, weight in enumerate(weights, 1):
        elapsed += weight
        t = elapsed / total
        eased = t*t*(3-2*t)
        x = start[0]+(end[0]-start[0])*eased
        y = start[1]+(end[1]-start[1])*eased + bend*math.sin(math.pi*t)
        points.append((x, min(.999, max(.001, y)), round(duration*t)))
    points[-1] = (*end, duration)
    return points


class LocalGapSolver:
    def __init__(self):
        self._model = None
        self._unavailable = False

    def _load(self):
        if self._unavailable:
            return None
        if self._model is None:
            try:
                # Must precede import: ORT 1.31 initializes telemetry on import.
                # Also prevents creation of its telemetry identifier on disk.
                os.environ['ORT_DISABLE_TELEMETRY'] = '1'
                import onnxruntime
                onnxruntime.disable_telemetry_events()
                from captcha_recognizer.slider import Slider
                self._model = Slider()
            except Exception:
                # Never print dependency exceptions; never download a model.
                self._unavailable = True
        return self._model

    def detect(self, png):
        """Two confident aligned boxes: piece and gap, in PNG coordinates.

        Require an unambiguous pair rather than trusting a single gap without a
        common piece reference. This also avoids treating gap x as drag distance.
        """
        model = self._load()
        if model is None:
            return None
        image = model.image_to_array(png)
        if image is None or not 1 <= image.shape[0]*image.shape[1] <= 4_000_000:
            return None
        results = model.predict(image, conf=.8, iou=.7, imgsz=640)
        if not results or len(results[0][0]) != 2:
            return None
        boxes = sorted((tuple(float(v) for v in box[:5]) for box in results[0][0]), key=lambda b: b[0])
        for x1, y1, x2, y2, score in boxes:
            if (not all(math.isfinite(v) for v in (x1, y1, x2, y2, score))
                    or not .8 <= score <= 1 or not 0 <= x1 < x2 <= image.shape[1]
                    or not 0 <= y1 < y2 <= image.shape[0]):
                return None
        piece, gap = boxes
        pw, ph = piece[2]-piece[0], piece[3]-piece[1]
        gw, gh = gap[2]-gap[0], gap[3]-gap[1]
        if (gap[0] <= piece[2] or not .8 <= pw/gw <= 1.25 or not .8 <= ph/gh <= 1.25
                or abs((piece[1]+piece[3]-gap[1]-gap[3])/2) > .2*ph):
            return None
        return piece, gap

    def solve(self, capture, challenge, cancel):
        if cancel.is_set() or challenge.get('kind') != 'gap_slider':
            return None
        try:
            rects = challenge['rects']
            handle, track, image, piece_rect = (rects[k] for k in
                                                ('slider_handle', 'slider_track', 'slider_image', 'slider_piece'))
            for rect in (handle, track, image, piece_rect):
                if (not isinstance(rect, dict) or any(type(rect.get(k)) not in (int, float)
                        or not math.isfinite(rect[k]) for k in ('x', 'y', 'width', 'height'))
                        or min(rect['width'], rect['height']) <= 0):
                    return None
            # The puzzle and handle can have different travel distances.
            piece_travel = image['width']-piece_rect['width']
            handle_travel = track['width']-handle['width']
            if min(piece_travel, handle_travel) <= 0:
                return None
            detection = self.detect(capture.png)
            if detection is None or cancel.is_set():
                return None
            piece, gap = detection
            meta = capture.metadata
            # Centers are the same reference for both predicted boxes.
            piece_x = meta.x0+(piece[0]+piece[2])/2/meta.x_scale
            gap_x = meta.x0+(gap[0]+gap[2])/2/meta.x_scale
            piece_y = meta.y0+(piece[1]+piece[3])/2/meta.y_scale
            gap_y = meta.y0+(gap[1]+gap[3])/2/meta.y_scale
            if (not piece_rect['x'] <= piece_x <= piece_rect['x']+piece_rect['width']
                    or not piece_rect['y'] <= piece_y <= piece_rect['y']+piece_rect['height']
                    or not image['x'] <= gap_x <= image['x']+image['width']
                    or not image['y'] <= gap_y <= image['y']+image['height']):
                return None
            distance = (gap_x-piece_x)*handle_travel/piece_travel
            sx, sy = handle['x']+handle['width']/2, handle['y']+handle['height']/2
            ex = sx+distance
            if not 0 < distance <= handle_travel or ex > track['x']+track['width']-handle['width']/2:
                return None
            start = ((sx-meta.x0)*meta.x_scale/meta.width, (sy-meta.y0)*meta.y_scale/meta.height)
            end = ((ex-meta.x0)*meta.x_scale/meta.width, start[1])
            return trajectory(start, end)
        except Exception:
            return None
