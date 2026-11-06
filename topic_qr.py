"""Extract a located QR from the hotspot authorization view in memory."""
from io import BytesIO
from math import ceil, floor


def _finder_corners(image, left, top, right, bottom):
    # shortcut: axis-aligned codes only, upgrade if the authorization page rotates them.
    gray = image.convert('L')
    width, height = right - left, bottom - top
    if min(width, height) < 21 or abs(width - height) > max(width, height) * .1:
        return False
    for modules in range(21, 178, 4):
        found = True
        for ox, oy in ((0, 0), (modules - 7, 0), (0, modules - 7)):
            mismatches = 0
            for y in range(7):
                for x in range(7):
                    px = left + int((ox + x + .5) * width / modules)
                    py = top + int((oy + y + .5) * height / modules)
                    dark = x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4)
                    value = gray.getpixel((px, py))
                    mismatches += (dark and value > 140) or (not dark and value < 170)
            if mismatches > 2:
                found = False
                break
        if found:
            return True
    return False


def extract_topic_qr(png):
    try:
        import zxingcpp
        from PIL import Image, ImageOps

        image = Image.open(BytesIO(png)).convert('RGB')
        if max(image.size) > 4096:
            return None
        enlarged = image.resize((image.width * 2, image.height * 2), Image.Resampling.NEAREST)
        codes = zxingcpp.read_barcodes(enlarged, formats=zxingcpp.BarcodeFormat.QRCode, return_errors=True)
        boxes = set()
        for code in codes:
            p = code.position
            points = (p.top_left, p.top_right, p.bottom_right, p.bottom_left)
            left, top = floor(min(p.x for p in points) / 2), floor(min(p.y for p in points) / 2)
            right, bottom = ceil(max(p.x for p in points) / 2), ceil(max(p.y for p in points) / 2)
            if left < 0 or top < 0 or right > image.width or bottom > image.height:
                continue
            if _finder_corners(image, left, top, right, bottom):
                boxes.add((left, top, right, bottom))
        if not boxes:
            return None
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[2] for box in boxes)
        bottom = max(box[3] for box in boxes)
        if any(max(abs(a - b) for a, b in zip(box, (left, top, right, bottom))) > 3 for box in boxes):
            return None
        crop = image.crop((left, top, right, bottom))
        crop = ImageOps.expand(crop, border=max(10, ceil(max(crop.size) * .1)), fill='white')
        scale = max(1, ceil(600 / min(crop.size)))
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.NEAREST)
        output = BytesIO()
        crop.save(output, format='PNG')
        return output.getvalue()
    except Exception:
        return None
