"""Programmatically generated offline slider image for tests/RSS measurement.

No saved image, real page, credentials, network or downloaded fixture assets.
"""


def gap_fixture():
    import cv2
    import numpy as np
    from login_driver import Capture, FrameMetadata
    y, x = np.mgrid[:180, :320]
    picture = np.zeros((240, 320, 3), np.uint8)
    picture[180:] = 235
    for channel in range(3):
        picture[:180, :, channel] = np.clip(
            110+45*np.sin(x/23+channel)+30*np.sin(y/11+channel)+25*np.cos((x+y)/35), 0, 255)
    mask = np.zeros((60, 60), np.uint8)
    cv2.rectangle(mask, (5, 15), (44, 54), 255, -1)
    cv2.circle(mask, (25, 15), 9, 255, -1)
    cv2.circle(mask, (44, 35), 9, 255, -1)
    cv2.circle(mask, (5, 35), 9, 0, -1)
    cv2.circle(mask, (25, 54), 9, 0, -1)
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    gap = picture[60:120, 200:260]
    patch = gap.copy()
    gap[mask > 0] = (gap[mask > 0]*.4).astype(np.uint8)
    cv2.drawContours(gap, contours, -1, (220, 220, 220), 1)
    piece = picture[60:120, 10:70]
    piece[mask > 0] = patch[mask > 0]
    cv2.drawContours(piece, contours, -1, (255, 255, 255), 2)
    cv2.rectangle(picture, (10, 200), (310, 224), (180, 180, 180), -1)
    cv2.rectangle(picture, (10, 200), (50, 224), (100, 100, 100), -1)
    ok, png = cv2.imencode('.png', picture)
    if not ok:
        raise RuntimeError('synthetic_image_failed')
    rects = dict(slider_handle=dict(x=10, y=200, width=40, height=24),
                 slider_track=dict(x=10, y=200, width=300, height=24),
                 slider_image=dict(x=0, y=0, width=320, height=180),
                 slider_piece=dict(x=10, y=60, width=60, height=60))
    return Capture(bytes(png), FrameMetadata(0, 0, 320, 240)), dict(kind='gap_slider', rects=rects)
