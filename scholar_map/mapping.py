from __future__ import annotations

from collections import Counter, defaultdict
from html import escape
from itertools import product
import json
import math
from pathlib import Path

import folium
from branca.colormap import LinearColormap

from .common import normalise, valid_coordinate, write_csv, write_json

EARTH_KM = 6371.0088
CLUSTER_FIELDS = ["cluster_id", "latitude", "longitude", "frequency", "raw_row_count",
                  "unique_citing_papers", "unique_institutions", "cities", "countries", "institutions"]
CONFIDENCE = {"low": 1, "medium": 2, "high": 3}
WORLD_BORDERS = Path(__file__).resolve().parent / "data" / "ne_110m_admin_0_countries.geojson"
COLOR_FREQUENCY_CAP = 10


def paper_identity(row):
    identifier = row.get("citing_doi") or row.get("openalex_work_id")
    if identifier:
        return identifier
    title = normalise(row.get("citing_paper_title", ""))
    return title + ":" + row.get("citing_paper_year", "") if title else row.get("citing_paper_id", "") or row.get("record_id", "")


def unit_vector(lat, lon):
    lat, lon = math.radians(lat), math.radians(lon)
    return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))


def cluster_indices(points, radius_km):
    """Connected components of points within a great-circle distance, using a 3D spatial hash."""
    if not 0 <= radius_km <= 1000:
        raise ValueError("--cluster-km must be between 0 and 1000.")
    if radius_km == 0:
        exact = defaultdict(list)
        for index, (lat, lon) in enumerate(points):
            lon = (lon + 180) % 360 - 180
            exact[(lat, 0 if abs(lat) == 90 else lon)].append(index)
        return list(exact.values())
    chord = 2 * math.sin(radius_km / (2 * EARTH_KM))
    threshold = chord * chord
    parents = list(range(len(points)))
    sizes = [1] * len(points)

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            if sizes[left] < sizes[right]:
                left, right = right, left
            parents[right] = left
            sizes[left] += sizes[right]

    cells = defaultdict(list)
    vectors = [unit_vector(*point) for point in points]
    for index, vector in enumerate(vectors):
        cell = tuple(math.floor(x / chord) for x in vector)
        for offset in product((-1, 0, 1), repeat=3):
            neighbor = tuple(x + dx for x, dx in zip(cell, offset))
            for other in cells[neighbor]:
                if sum((x - y) ** 2 for x, y in zip(vector, vectors[other])) <= threshold + 1e-15:
                    union(index, other)
        cells[cell].append(index)
    groups = defaultdict(list)
    for index in range(len(points)):
        groups[find(index)].append(index)
    return list(groups.values())


def aggregate(rows, radius_km=25, count_mode="rows", min_confidence="medium"):
    if count_mode not in {"rows", "paper-institution", "unique-papers"}:
        raise ValueError("Invalid count mode")
    if min_confidence not in CONFIDENCE:
        raise ValueError("Invalid confidence threshold")
    valid, excluded = [], Counter()
    for row in rows:
        if row.get("geocode_status") != "resolved":
            excluded["unresolved"] += 1
        elif not valid_coordinate(row.get("latitude"), row.get("longitude")):
            excluded["invalid_coordinate"] += 1
        elif CONFIDENCE.get(row.get("geocode_confidence", ""), 0) < CONFIDENCE[min_confidence]:
            excluded["low_confidence"] += 1
        else:
            valid.append(row)
    # Cluster distinct coordinates only; repeated affiliation rows stay available for counting.
    coordinate_rows = defaultdict(list)
    for row in valid:
        coordinate_rows[(float(row["latitude"]), float(row["longitude"]))].append(row)
    points = list(coordinate_rows)
    groups = cluster_indices(points, radius_km)
    clusters = []
    for members in groups:
        records = [row for index in members for row in coordinate_rows[points[index]]]
        weights = [len(coordinate_rows[points[index]]) for index in members]
        vectors = [unit_vector(*points[index]) for index in members]
        centroid = [sum(vector[axis] * weight for vector, weight in zip(vectors, weights)) for axis in range(3)]
        lat = math.degrees(math.atan2(centroid[2], math.hypot(centroid[0], centroid[1])))
        lon = math.degrees(math.atan2(centroid[1], centroid[0]))
        papers = {paper_identity(row) for row in records}
        institutions = {normalise(row.get("institution", "")) for row in records}
        if count_mode == "rows":
            frequency = len(records)
        elif count_mode == "unique-papers":
            frequency = len(papers)
        else:
            frequency = len({(row.get("source_paper_id", ""), paper_identity(row),
                              row.get("institution_id") or normalise(row.get("institution", ""))) for row in records})
        join = lambda key: " | ".join(sorted({row.get(key, "") for row in records if row.get(key)}))
        clusters.append({"latitude": round(lat, 6), "longitude": round(lon, 6), "frequency": frequency,
                         "raw_row_count": len(records), "unique_citing_papers": len(papers),
                         "unique_institutions": len(institutions), "cities": join("city"), "countries": join("country"),
                         "institutions": join("institution")})
    clusters.sort(key=lambda cluster: (-cluster["frequency"], cluster["latitude"], cluster["longitude"]))
    for index, cluster in enumerate(clusters, 1):
        cluster["cluster_id"] = index
    report = {"input_rows": len(rows), "mapped_rows": len(valid), "excluded": dict(excluded),
              "cluster_count": len(clusters), "cluster_km": radius_km, "count_mode": count_mode,
              "min_confidence": min_confidence, "total_frequency": sum(c["frequency"] for c in clusters)}
    return clusters, report


def create_map(rows, output_dir, radius_km=25, count_mode="rows", min_confidence="medium", color_scale="linear"):
    clusters, report = aggregate(rows, radius_km, count_mode, min_confidence)
    if color_scale not in {"linear", "log"}:
        raise ValueError("Invalid color scale")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "coordinate_frequency.csv", clusters, CLUSTER_FIELDS)
    report["color_scale"] = color_scale
    report["color_frequency_cap"] = COLOR_FREQUENCY_CAP
    report["basemap"] = "embedded_natural_earth_110m"
    write_json(output_dir / "map_report.json", report)
    world = folium.Map(location=[20, 0], zoom_start=2, min_zoom=1, max_zoom=8,
                       tiles=None, prefer_canvas=True, world_copy_jump=False)
    world.get_root().header.add_child(folium.Element(
        "<style>.leaflet-container{background:#dceaf4}</style>"))
    # Embed a small Natural Earth vector layer. This makes the basemap independent
    # of public tile servers, which may return a 403 or impose usage limits.
    borders = json.loads(WORLD_BORDERS.read_text(encoding="utf-8"))
    folium.GeoJson(
        borders, name="Natural Earth world boundaries", smooth_factor=1.5,
        style_function=lambda _feature: {
            "fillColor": "#f5f3e8", "fillOpacity": 1, "color": "#a6af9b", "weight": 0.6,
        },
    ).add_to(world)
    transform = math.log1p if color_scale == "log" else float
    colors = ["#c6dbef", "#9ecae1", "#6baed6", "#3182bd", "#08519c", "#08306b"]
    # Legend ticks stay in raw frequency units even when circle colors use log scaling.
    index = [((math.expm1(i / 5 * math.log1p(COLOR_FREQUENCY_CAP))) if color_scale == "log" else i / 5 * COLOR_FREQUENCY_CAP)
             for i in range(6)]
    colormap = LinearColormap(colors, index=index, vmin=0, vmax=COLOR_FREQUENCY_CAP,
                             caption="Frequency (10+ = darkest)")
    colormap.add_to(world)
    # Draw lower frequencies first, so higher-frequency circles remain on top.
    for cluster in sorted(clusters, key=lambda item: item["frequency"]):
        city = escape(cluster["cities"] or "Unknown city")
        institution_lines = "<br>".join(escape(x) for x in cluster["institutions"].split(" | ")[:15])
        popup = (f"<b>{city}</b><br>{escape(cluster['countries'])}<hr>"
                 f"Frequency: <b>{cluster['frequency']}</b><br>"
                 f"Affiliation rows: {cluster['raw_row_count']}<br>"
                 f"Citing papers: {cluster['unique_citing_papers']}<br>"
                 f"Institutions: {cluster['unique_institutions']}<hr>{institution_lines}")
        # Use a continuous log color when requested rather than log-spaced piecewise interpolation.
        capped_frequency = min(cluster["frequency"], COLOR_FREQUENCY_CAP)
        color = LinearColormap(colors, vmin=0, vmax=transform(COLOR_FREQUENCY_CAP))(transform(capped_frequency))
        folium.CircleMarker(location=[cluster["latitude"], cluster["longitude"]], radius=8,
                            color="#ffffff", weight=1, fill=True, fill_color=color, fill_opacity=0.9,
                            tooltip=f"{city}: {cluster['frequency']}", popup=folium.Popup(popup, max_width=420)).add_to(world)
    mode_label = {"rows": "author affiliation occurrences", "paper-institution": "paper–institution occurrences", "unique-papers": "distinct citing papers"}[count_mode]
    excluded_count = len(rows) - report["mapped_rows"]
    empty = "<div style='color:#b45309;margin-top:8px'>No resolved coordinates to display</div>" if not clusters else ""
    panel = f"""<div style="position:fixed;top:18px;left:56px;z-index:9999;background:white;
       padding:18px 22px;border-radius:10px;box-shadow:0 3px 18px #0002;font-family:Arial,sans-serif;
       color:#17324d;max-width:340px"><div style="font-size:22px;font-weight:700">Citation geography</div>
       <div style="font-size:13px;line-height:1.7;margin-top:8px">{len(clusters):,} locations · {report['mapped_rows']:,} affiliation rows<br>
       Color shows {mode_label}<br>Nearby points grouped within {radius_km:g} km<br>
       {excluded_count:,} rows without eligible coordinates{empty}
       <div style="font-size:10px;color:#64748b;margin-top:8px">Basemap: Natural Earth (public domain)</div></div></div>"""
    world.get_root().html.add_child(folium.Element(panel))
    world.save(str(output_dir / "world_map.html"))
    return report
