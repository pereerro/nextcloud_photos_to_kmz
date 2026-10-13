#!/usr/bin/env python3
"""
Generate a KMZ route track with start/end thumbnails from geotagged photos.
Organizes waypoints into stages based on calendar days.
"""

import argparse
import io
import math
import os
import sys
import zipfile
from datetime import datetime
from PIL import Image, ExifTags

# Environment variable defaults
DEFAULT_PHOTOS_DIR = os.getenv("PHOTOS_DIR", "/photos")
DEFAULT_OUTPUT_KMZ = os.getenv("OUTPUT_KMZ", "/photos/routes.kmz")
DEFAULT_THUMB_SIZE = int(os.getenv("THUMBNAIL_SIZE", "320"))
DEFAULT_MIN_DISTANCE_M = float(os.getenv("MIN_DISTANCE_METERS", "10.0"))


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points in meters.
    """
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius_m * c


def dms_to_decimal(dms_values, ref: str) -> float:
    """
    Convert EXIF GPS DMS (degrees, minutes, seconds) rational tuples to decimal degrees.
    """
    degrees = float(dms_values[0])
    minutes = float(dms_values[1])
    seconds = float(dms_values[2])
    decimal_degrees = degrees + (minutes / 60.0) + (seconds / 3600.0)

    if ref.upper() in ["S", "W"]:
        decimal_degrees = -decimal_degrees
    return decimal_degrees


def extract_photo_metadata(filepath: str):
    """
    Extract capture timestamp and decimal GPS coordinates from photo EXIF tags.
    Returns a dictionary or None if mandatory tags are missing.
    """
    try:
        with Image.open(filepath) as img:
            exif = img.getexif()
            if not exif:
                return None

            # Attempt to locate DateTimeOriginal
            capture_time_raw = None
            for tag_id, val in exif.items():
                tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                if tag_name == "DateTimeOriginal":
                    capture_time_raw = val
                    break

            # Search in the Exif sub-IFD (0x8769) if not present in the main IFD
            if not capture_time_raw and 0x8769 in exif:
                sub_ifd = exif.get_ifd(0x8769)
                capture_time_raw = (
                    sub_ifd.get(0x9003)
                    or sub_ifd.get(0x9004)
                    or sub_ifd.get(0x0132)
                )

            if not capture_time_raw and 0x0132 in exif:
                capture_time_raw = exif[0x0132]

            if not capture_time_raw:
                return None

            try:
                timestamp = datetime.strptime(str(capture_time_raw).strip(), "%Y:%m:%d %H:%M:%S")
            except ValueError:
                return None

            # GPS IFD is located at tag 0x8825
            gps_ifd = exif.get_ifd(0x8825)
            if not gps_ifd:
                return None

            lat_dms = gps_ifd.get(2)
            lat_ref = gps_ifd.get(1)
            lon_dms = gps_ifd.get(4)
            lon_ref = gps_ifd.get(3)

            if not (lat_dms and lat_ref and lon_dms and lon_ref):
                return None

            latitude = dms_to_decimal(lat_dms, str(lat_ref))
            longitude = dms_to_decimal(lon_dms, str(lon_ref))

            return {
                "filepath": filepath,
                "filename": os.path.basename(filepath),
                "timestamp": timestamp,
                "latitude": latitude,
                "longitude": longitude,
            }
    except Exception:
        return None


def generate_thumbnail_bytes(filepath: str, max_dimension: int) -> bytes:
    """
    Generate an in-memory downscaled JPEG thumbnail.
    """
    with Image.open(filepath) as img:
        img.thumbnail((max_dimension, max_dimension))
        output_buffer = io.BytesIO()
        img.save(output_buffer, format="JPEG", quality=75, optimize=True)
        return output_buffer.getvalue()


def filter_redundant_waypoints(photos: list, min_distance_m: float) -> list:
    """
    Filter out consecutive waypoints that are closer than min_distance_m,
    preserving the initial and final photos of the stage.
    """
    if len(photos) <= 2 or min_distance_m <= 0:
        return photos

    filtered = [photos[0]]
    for photo in photos[1:-1]:
        prev = filtered[-1]
        dist = haversine_distance(
            prev["latitude"], prev["longitude"],
            photo["latitude"], photo["longitude"]
        )
        if dist >= min_distance_m:
            filtered.append(photo)

    filtered.append(photos[-1])
    return filtered


def build_kmz(photos_dir: str, output_kmz: str, thumb_size: int, min_distance: float, ignore_prefix: str):
    """
    Scan photos, group into daily stages, generate KML markup, and bundle the KMZ.
    """
    if not os.path.isdir(photos_dir):
        print(f"Error: Photos directory not found at '{photos_dir}'", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning photos in: {photos_dir}")
    valid_photos = []

    for root, _, files in os.walk(photos_dir):
        for filename in sorted(files):
            # Check ignore prefix (e.g. discard discarded photos starting with '_')
            if ignore_prefix and filename.startswith(ignore_prefix):
                continue

            if filename.lower().endswith((".jpg", ".jpeg")):
                full_path = os.path.join(root, filename)
                metadata = extract_photo_metadata(full_path)
                if metadata:
                    valid_photos.append(metadata)

    if not valid_photos:
        print("No valid geotagged photos found. Exiting.")
        return

    # Chronological sort
    valid_photos.sort(key=lambda item: item["timestamp"])

    # Group by calendar date (YYYY-MM-DD)
    stages = {}
    for photo in valid_photos:
        day_key = photo["timestamp"].strftime("%Y-%m-%d")
        stages.setdefault(day_key, []).append(photo)

    print(f"Total geotagged images: {len(valid_photos)}")
    print(f"Detected stages/days: {len(stages)}")

    kml_elements = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        "  <name>Camins de Ronda - Route Tracks</name>",
        '  <Style id="routeTrackStyle">',
        "    <LineStyle>",
        "      <color>ff1345d1</color>",  # Red/Orange in AABBGGRR format
        "      <width>4</width>",
        "    </LineStyle>",
        "  </Style>",
    ]

    thumbnail_archive_payloads = {}

    for stage_date, photos in stages.items():
        if len(photos) < 2:
            print(f"Notice: Date {stage_date} has fewer than 2 points. Skipping track generation.")
            continue

        cleaned_photos = filter_redundant_waypoints(photos, min_distance)
        first_point = cleaned_photos[0]
        last_point = cleaned_photos[-1]

        kml_elements.append(f"  <Folder><name>Stage {stage_date}</name>")

        # 1. Line track
        coord_tokens = [f"{p['longitude']},{p['latitude']},0" for p in cleaned_photos]
        coords_str = " ".join(coord_tokens)

        kml_elements.extend([
            "    <Placemark>",
            f"      <name>Track {stage_date}</name>",
            "      <styleUrl>#routeTrackStyle</styleUrl>",
            "      <LineString>",
            "        <tessellate>1</tessellate>",
            f"        <coordinates>{coords_str}</coordinates>",
            "      </LineString>",
            "    </Placemark>",
        ])

        # 2. Stage endpoints (Start / Finish)
        milestones = [
            ("Start", first_point),
            ("Finish", last_point),
        ]

        for milestone_type, pt in milestones:
            image_filename = f"thumb_{stage_date}_{milestone_type.lower()}_{pt['filename']}"
            relative_image_path = f"images/{image_filename}"

            if relative_image_path not in thumbnail_archive_payloads:
                thumbnail_archive_payloads[relative_image_path] = generate_thumbnail_bytes(
                    pt["filepath"], thumb_size
                )

            time_str = pt["timestamp"].strftime("%H:%M:%S")
            popup_html = (
                f"<![CDATA["
                f"<h3>{milestone_type} of Stage ({stage_date})</h3>"
                f"<p><b>Time:</b> {time_str}</p>"
                f"<p><b>Original file:</b> {pt['filename']}</p>"
                f'<img src="{relative_image_path}" width="{thumb_size}" /><br/>'
                f"]]>"
            )

            kml_elements.extend([
                "    <Placemark>",
                f"      <name>{milestone_type} - {stage_date} ({time_str})</name>",
                f"      <description>{popup_html}</description>",
                "      <Point>",
                f"        <coordinates>{pt['longitude']},{pt['latitude']},0</coordinates>",
                "      </Point>",
                "    </Placemark>",
            ])

        kml_elements.append("  </Folder>")

    kml_elements.extend(["</Document>", "</kml>"])
    kml_bytes = "\n".join(kml_elements).encode("utf-8")

    # Create destination directory if needed
    output_dir = os.path.dirname(output_kmz)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    with zipfile.ZipFile(output_kmz, "w", zipfile.ZIP_DEFLATED) as kmz:
        kmz.writestr("doc.kml", kml_bytes)
        for img_path, img_data in thumbnail_archive_payloads.items():
            kmz.writestr(img_path, img_data)

    print(f"\nKMZ bundle successfully created: {os.path.abspath(output_kmz)}")
    print(f"File size: {round(os.path.getsize(output_kmz) / 1024, 2)} KB")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Convert GPS photo folders into a daily stage KMZ track file."
    )
    parser.add_argument(
        "-i", "--input-dir",
        default=DEFAULT_PHOTOS_DIR,
        help=f"Directory containing source photos (default: {DEFAULT_PHOTOS_DIR})"
    )
    parser.add_argument(
        "-o", "--output-kmz",
        default=DEFAULT_OUTPUT_KMZ,
        help=f"Output .kmz file path (default: {DEFAULT_OUTPUT_KMZ})"
    )
    parser.add_argument(
        "-s", "--thumb-size",
        type=int,
        default=DEFAULT_THUMB_SIZE,
        help=f"Max dimension in pixels for embedded thumbnails (default: {DEFAULT_THUMB_SIZE})"
    )
    parser.add_argument(
        "-d", "--min-distance",
        type=float,
        default=DEFAULT_MIN_DISTANCE_M,
        help=f"Minimum distance in meters between waypoints to filter noise (default: {DEFAULT_MIN_DISTANCE_M})"
    )
    parser.add_argument(
        "--ignore-prefix",
        default="_",
        help="Ignore image files starting with this prefix (default: '_')"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    build_kmz(
        photos_dir=args.input_dir,
        output_kmz=args.output_kmz,
        thumb_size=args.thumb_size,
        min_distance=args.min_distance,
        ignore_prefix=args.ignore_prefix
    )