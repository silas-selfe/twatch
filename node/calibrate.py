"""Ground-plane calibration: map each camera's view onto one top-down
reference image (satellite screenshot / site plan), the shared "physical
space" that cross-camera tracking runs in.

Per camera, click pairs of corresponding GROUND points -- a spot where
pavement meets a pole base, a lane-line end, a manhole -- first in the
camera image, then the same physical spot in the reference image. 4+ pairs
give a homography H (camera pixels -> reference pixels). Points on the
ground only: anything with height (car roofs, fence tops) breaks the
plane assumption.

  python calibrate.py --image discovered/ch1.jpg --ref discovered/ref.png --id cam1
  python calibrate.py --composite --ref discovered/ref.png   # all cameras on the map

Keys: click = add point (camera, then reference) | u = undo | s = save | q = quit.

Output: cameras/<id>/calibration.yaml (points + H + reprojection error,
no credentials -- safe to share) and warp_preview.jpg, the camera image
warped onto the reference at 50% blend: if the streets line up, the
calibration is good. Known caveat: barrel distortion on wide-angle cams
bends the fit near frame edges -- click points spread across the ROAD
area you care about, not the extreme corners.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
MAX_W = 1200  # display scale cap; clicks are stored in original pixels


class ClickView:
    """One window showing an image scaled to fit; maps clicks back to
    original-resolution coordinates and redraws numbered markers."""

    def __init__(self, name: str, img: np.ndarray):
        self.name = name
        self.img = img
        self.scale = min(1.0, MAX_W / img.shape[1])
        self.points: list[tuple[float, float]] = []
        cv2.namedWindow(name, cv2.WINDOW_AUTOSIZE)

    def draw(self, active: bool):
        disp = cv2.resize(self.img, None, fx=self.scale, fy=self.scale) \
            if self.scale < 1.0 else self.img.copy()
        for i, (x, y) in enumerate(self.points):
            p = (int(x * self.scale), int(y * self.scale))
            cv2.circle(disp, p, 6, (40, 40, 220), 2)
            cv2.putText(disp, str(i + 1), (p[0] + 8, p[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (40, 40, 220), 2)
        banner = f"{self.name}: CLICK HERE ({len(self.points)} pts)" if active \
            else f"{self.name}: {len(self.points)} pts"
        cv2.putText(disp, banner, (10, 28), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (0, 200, 255), 2)
        cv2.imshow(self.name, disp)

    def add(self, x: int, y: int):
        self.points.append((x / self.scale, y / self.scale))


def compute_h(pts_img, pts_ref):
    """Homography + per-point reprojection error in reference pixels."""
    a = np.float64(pts_img).reshape(-1, 1, 2)
    b = np.float64(pts_ref).reshape(-1, 1, 2)
    method = cv2.RANSAC if len(pts_img) > 5 else 0
    H, _ = cv2.findHomography(a, b, method, 5.0)
    if H is None:
        return None, None
    proj = cv2.perspectiveTransform(a, H)
    err = np.linalg.norm(proj - b, axis=2).ravel()
    return H, err


def trusted_mask(pts_ref, shape, pad: float = 1.25):
    """The homography is only trusted near the clicked region: the convex
    hull of the reference points, padded a bit. Outside it (especially the
    camera's far field) the warp smears whole blocks and means nothing."""
    h, w = shape[:2]
    hull = cv2.convexHull(np.float32(pts_ref).reshape(-1, 1, 2))
    c = hull.reshape(-1, 2).mean(axis=0)
    hull = (c + (hull.reshape(-1, 2) - c) * pad).astype(np.int32)
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [hull], 255)
    return mask.astype(bool)


def warp_preview(cam_img, ref_img, H, out_path: Path, pts_ref=None):
    """Camera view warped onto the reference, 50/50 blend where it lands --
    the eyeball test: do lane lines and curbs coincide? Clipped to the
    trusted (clicked) region when reference points are given."""
    h, w = ref_img.shape[:2]
    warped = cv2.warpPerspective(cam_img, H, (w, h))
    mask = warped.sum(axis=2) > 0
    if pts_ref is not None:
        mask &= trusted_mask(pts_ref, ref_img.shape)
    blend = np.where(mask[..., None],
                     (ref_img * 0.5 + warped * 0.5).astype(np.uint8), ref_img)
    cv2.imwrite(str(out_path), blend, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return out_path


def diagnostic(ref_img, pts_ref, pts_img, H, out_path: Path):
    """Green = where you clicked on the reference; red = where H sends the
    matching camera click. Long yellow lines = suspect pairs."""
    diag = ref_img.copy()
    proj = cv2.perspectiveTransform(
        np.float64(pts_img).reshape(-1, 1, 2), H).reshape(-1, 2)
    for i, ((gx, gy), (rx, ry)) in enumerate(zip(pts_ref, proj)):
        cv2.line(diag, (int(gx), int(gy)), (int(rx), int(ry)), (0, 200, 255), 2)
        cv2.circle(diag, (int(gx), int(gy)), 5, (0, 200, 0), -1)
        cv2.circle(diag, (int(rx), int(ry)), 5, (0, 0, 230), -1)
        cv2.putText(diag, str(i + 1), (int(gx) + 8, int(gy) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 0), 2)
    cv2.imwrite(str(out_path), diag, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return out_path


def calibrate(image: str, ref: str, cam_id: str):
    cam_img = cv2.imread(image)
    ref_img = cv2.imread(ref)
    if cam_img is None:
        sys.exit(f"cannot read camera image {image}")
    if ref_img is None:
        sys.exit(f"cannot read reference image {ref}")

    cam = ClickView(f"camera {cam_id}", cam_img)
    refv = ClickView("reference", ref_img)

    # alternate strictly -- camera point, then its reference match -- so a
    # stray click in the wrong window can't silently shift all later pairs
    def on_click(view):
        def cb(event, x, y, flags, _):
            if event != cv2.EVENT_LBUTTONDOWN:
                return
            turn = cam if len(cam.points) == len(refv.points) else refv
            if view is turn:
                view.add(x, y)
        return cb

    cv2.setMouseCallback(cam.name, on_click(cam))
    cv2.setMouseCallback(refv.name, on_click(refv))

    print(__doc__.split("Keys:")[0])
    print("Keys: u = undo | s = save (needs 4+ pairs) | q = quit\n")
    while True:
        cam_active = len(cam.points) == len(refv.points)
        cam.draw(cam_active)
        refv.draw(not cam_active)
        k = cv2.waitKey(30) & 0xFF
        if k == ord("q"):
            cv2.destroyAllWindows()
            return
        if k == ord("u"):
            if len(refv.points) == len(cam.points) and cam.points:
                refv.points.pop()
            elif cam.points:
                cam.points.pop()
        if k == ord("s"):
            n = min(len(cam.points), len(refv.points))
            if n < 4:
                print(f"need 4+ complete pairs, have {n}")
                continue
            H, err = compute_h(cam.points[:n], refv.points[:n])
            if H is None:
                print("homography failed -- are points collinear? add spread")
                continue
            d = HERE / "cameras" / cam_id
            d.mkdir(parents=True, exist_ok=True)
            out = d / "calibration.yaml"
            out.write_text(yaml.safe_dump({
                "id": cam_id,
                "image": str(Path(image).resolve()),
                "ref": str(Path(ref).resolve()),
                "image_size": [cam_img.shape[1], cam_img.shape[0]],
                "points_image": [list(p) for p in cam.points[:n]],
                "points_ref": [list(p) for p in refv.points[:n]],
                "H": H.tolist(),
                "reproj_err_px": {"mean": float(err.mean()),
                                  "max": float(err.max())},
                # set later by measuring a known distance on the reference
                "meters_per_ref_px": None,
            }, sort_keys=False))
            prev = warp_preview(cam_img, ref_img, H, d / "warp_preview.jpg",
                                refv.points[:n])
            diagnostic(ref_img, refv.points[:n], cam.points[:n], H,
                       d / "diagnostic.jpg")
            print(f"saved {out}")
            print(f"  reprojection error: mean {err.mean():.1f}px, "
                  f"max {err.max():.1f}px (on the reference image)")
            print(f"  eyeball check: {prev}")
            cv2.destroyAllWindows()
            return


def composite(ref: str):
    """Every calibrated camera warped onto the reference in one image --
    the first rendering of the shared physical space."""
    ref_img = cv2.imread(ref)
    if ref_img is None:
        sys.exit(f"cannot read reference image {ref}")
    h, w = ref_img.shape[:2]
    acc = np.zeros((h, w, 3), np.float64)
    cnt = np.zeros((h, w, 1), np.float64)
    labels = []
    for cal in sorted((HERE / "cameras").glob("*/calibration.yaml")):
        c = yaml.safe_load(cal.read_text())
        img = cv2.imread(c["image"])
        if img is None:
            print(f"skip {c['id']}: camera image missing ({c['image']})")
            continue
        warped = cv2.warpPerspective(img, np.float64(c["H"]), (w, h))
        m = ((warped.sum(axis=2) > 0)
             & trusted_mask(c["points_ref"], ref_img.shape))
        m = m[..., None].astype(np.float64)
        acc += warped * m
        cnt += m
        ys, xs = np.nonzero(m[..., 0])
        if len(xs):
            labels.append((c["id"], int(xs.mean()), int(ys.mean())))
        print(f"  {c['id']}: reproj mean "
              f"{c['reproj_err_px']['mean']:.1f}px, footprint "
              f"{int(m.sum())}px")
    if not labels:
        sys.exit("no calibrated cameras found under cameras/*/calibration.yaml")
    covered = cnt[..., 0] > 0
    out_img = (ref_img * 0.35).astype(np.float64)
    out_img[covered] = (acc[covered] / cnt[covered]) * 0.65 \
        + ref_img[covered] * 0.35
    out_img = out_img.astype(np.uint8)
    for cam_id, x, y in labels:
        cv2.putText(out_img, cam_id, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (0, 200, 255), 2)
    out = HERE / "discovered" / "composite.jpg"
    out.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(out), out_img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"wrote {out} -- dim = unseen by any camera")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", help="camera still to calibrate")
    ap.add_argument("--ref", required=True,
                    help="top-down reference image (satellite screenshot)")
    ap.add_argument("--id", help="camera id, e.g. cam1 (-> cameras/<id>/)")
    ap.add_argument("--composite", action="store_true",
                    help="render all calibrated cameras onto the reference")
    args = ap.parse_args()
    if args.composite:
        composite(args.ref)
    elif args.image and args.id:
        calibrate(args.image, args.ref, args.id)
    else:
        ap.error("either --composite, or both --image and --id")


if __name__ == "__main__":
    main()
