"""Extract a verified QR from the hotspot authorization view in memory."""
from io import BytesIO
from math import ceil, floor


def extract_topic_qr(png):
    try:
        import zxingcpp
        from PIL import Image, ImageOps

        image = Image.open(BytesIO(png)).convert('RGB')
        if max(image.size) > 4096:
            return None
        # The decoder sometimes needs larger modules to read the central logo.
        enlarged = image.resize((image.width * 2, image.height * 2), Image.Resampling.NEAREST)
        codes = zxingcpp.read_barcodes(enlarged, formats=zxingcpp.BarcodeFormat.QRCode)
        if len(codes) != 1 or not codes[0].valid:
            return None
        p = codes[0].position
        points = (p.top_left, p.top_right, p.bottom_right, p.bottom_left)
        left, top = floor(min(p.x for p in points) / 2), floor(min(p.y for p in points) / 2)
        right, bottom = ceil(max(p.x for p in points) / 2), ceil(max(p.y for p in points) / 2)
        if left < 0 or top < 0 or right > image.width or bottom > image.height or min(right-left, bottom-top) < 21:
            return None
        crop = image.crop((left, top, right, bottom))
        crop = ImageOps.expand(crop, border=max(10, ceil(max(crop.size) * .1)), fill='white')
        scale = max(1, ceil(600 / min(crop.size)))
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.NEAREST)
        check = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.NEAREST)
        checked = zxingcpp.read_barcodes(check, formats=zxingcpp.BarcodeFormat.QRCode)
        if len(checked) != 1 or not checked[0].valid or checked[0].bytes != codes[0].bytes:
            return None
        output = BytesIO()
        crop.save(output, format='PNG')
        return output.getvalue()
    except Exception:
        return None
