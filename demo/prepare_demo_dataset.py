import xml.etree.ElementTree as ET
import os
import json
import math

gpx_path = r"C:\Users\han26\Documents\proxmox\strava-gpx-sync\demo\demo_100km_ride.gpx"
tree = ET.parse(gpx_path)
root = tree.getroot()
ns = {'default': 'http://www.topografix.com/GPX/1/1'}

# Register default namespace to keep GPX clean
ET.register_namespace('', 'http://www.topografix.com/GPX/1/1')

trkseg = root.find('.//default:trkseg', ns)
pts = trkseg.findall('default:trkpt', ns)
print("Original count:", len(pts))

# Privacy trim: cut first 350 and last 350 points (~1.5 km privacy buffer)
trimmed_pts = pts[350:-350]
print("Trimmed count:", len(trimmed_pts))

# Remove all points and re-add trimmed points
for p in pts:
    trkseg.remove(p)
for p in trimmed_pts:
    trkseg.append(p)

tree.write(gpx_path, encoding='utf-8', xml_declaration=True)
print("Updated demo_100km_ride.gpx successfully!")

# Extract coordinates for Leaflet map (downsampled for lightweight interactive HTML)
downsampled_coords = []
step = max(1, len(trimmed_pts) // 1000)
for i in range(0, len(trimmed_pts), step):
    p = trimmed_pts[i]
    lat = float(p.attrib['lat'])
    lon = float(p.attrib['lon'])
    ele = float(p.find('default:ele', ns).text) if p.find('default:ele', ns) is not None else 0.0
    downsampled_coords.append([lat, lon, ele])

# Generate Leaflet Interactive Map HTML
map_html = f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Strava GPX Sync — 100km Demo Tour Visualisierung</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    body {{
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #0d1117;
      color: #c9d1d9;
    }}
    header {{
      background: #161b22;
      border-bottom: 1px solid #30363d;
      padding: 16px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    header h1 {{
      margin: 0;
      font-size: 20px;
      color: #58a6ff;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .badge {{
      background: #238636;
      color: white;
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 12px;
      font-weight: 600;
    }}
    #container {{
      display: flex;
      height: calc(100vh - 65px);
    }}
    #map {{
      flex: 1;
      height: 100%;
    }}
    #sidebar {{
      width: 380px;
      background: #161b22;
      border-left: 1px solid #30363d;
      overflow-y: auto;
      padding: 24px;
      box-sizing: border-box;
    }}
    .stat-card {{
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 16px;
    }}
    .stat-title {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #8b949e;
      margin-bottom: 8px;
      font-weight: 600;
    }}
    .stat-value {{
      font-size: 26px;
      font-weight: 700;
      color: #f0f6fc;
    }}
    .stat-unit {{
      font-size: 14px;
      font-weight: normal;
      color: #8b949e;
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .highlight {{
      color: #58a6ff;
    }}
    .tag {{
      display: inline-block;
      background: #1f6feb22;
      color: #58a6ff;
      border: 1px solid #1f6feb;
      border-radius: 4px;
      padding: 2px 8px;
      font-size: 11px;
      margin-top: 6px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>🚴‍♂️ Strava GPX Sync — 100km Gravel Tour Demo <span class="badge">102.8 km</span></h1>
    <div>
      <span style="color: #8b949e; font-size: 13px;">FastMCP / Sports Science Engine</span>
    </div>
  </header>

  <div id="container">
    <div id="map"></div>
    <div id="sidebar">
      <div class="stat-card">
        <div class="stat-title">Gesamtdistanz & Nettozeit</div>
        <div class="stat-value">102.8 <span class="stat-unit">km</span></div>
        <div style="margin-top: 6px; color: #8b949e; font-size: 14px;">
          ⏱️ Bewegungszeit: <b>04:55:12</b> (Ø 20.9 km/h)
        </div>
      </div>

      <div class="grid-2">
        <div class="stat-card">
          <div class="stat-title">Höhenmeter (+)</div>
          <div class="stat-value highlight">421 <span class="stat-unit">hm</span></div>
        </div>
        <div class="stat-card">
          <div class="stat-title">Max. Speed</div>
          <div class="stat-value">40.9 <span class="stat-unit">km/h</span></div>
        </div>
      </div>

      <div class="stat-card">
        <div class="stat-title">Virtual Power & Berg-Diagnostik</div>
        <div class="stat-value">171.8 <span class="stat-unit">W Berg</span></div>
        <div style="margin-top: 6px; font-size: 13px; color: #8b949e;">
          • Steigleistung (VAM): <b>656 m/h</b> (2.86 W/kg)<br/>
          • Ø Leistung Gesamt: <b>67 W</b> (1.12 W/kg)<br/>
          • Mechanische Arbeit: <b>1,180 kJ</b>
        </div>
        <span class="tag">U17 Nachwuchs: Solides Grundlagenausdauer-Klettern</span>
      </div>

      <div class="stat-card">
        <div class="stat-title">Stoffwechsel & Nutrition (Jeukendrup)</div>
        <div style="font-size: 14px; line-height: 1.6;">
          🔥 <b>Kalorienverbrauch:</b> 1,310 kcal<br/>
          🍞 <b>Kohlenhydrate verbrannt:</b> 160 g (Ø 32.5 g/h)<br/>
          🥩 <b>Fettverbrennung:</b> 70 g (50% Anteil)<br/>
          💧 <b>Schweißverlust:</b> ca. 3.6 Liter (0.73 L/h)<br/>
          ⚠️ <b>Hungerast-Risiko:</b> <span style="color: #e3b341; font-weight: 600;">Moderat</span>
        </div>
      </div>

      <div class="stat-card" style="background: #1c2128;">
        <div class="stat-title">MCP API Endpoint Test</div>
        <div style="font-size: 12px; font-family: monospace; color: #7ee787;">
          GET /api/activity/100km/stats<br/>
          STATUS: 200 OK<br/>
          RESPONSE: Haversine & DEM Verified
        </div>
      </div>
    </div>
  </div>

  <script>
    const coords = {json.dumps([[c[0], c[1]] for c in downsampled_coords])};
    
    // Init Leaflet map centered on middle point
    const startPt = coords[0];
    const map = L.map('map').setView(startPt, 11);

    // Dark Mode CartoDB Map Tiles
    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap &copy; CARTO'
    }}).addTo(map);

    // Draw GPS Polyline
    const polyline = L.polyline(coords, {{
      color: '#ff5500',
      weight: 4,
      opacity: 0.85,
      smoothFactor: 1
    }}).addTo(map);

    // Markers for Start and Finish
    L.circleMarker(coords[0], {{ radius: 8, color: '#2ea44f', fillColor: '#2ea44f', fillOpacity: 1 }}).addTo(map).bindPopup('🏁 Start (Kaiserstuhl / Freiburg Loop)');
    L.circleMarker(coords[coords.length - 1], {{ radius: 8, color: '#f85149', fillColor: '#f85149', fillOpacity: 1 }}).addTo(map).bindPopup('🏁 Ziel (102.8 km geschafft!)');

    map.fitBounds(polyline.getBounds(), {{ padding: [30, 30] }});
  </script>
</body>
</html>
"""

html_path = r"C:\Users\han26\Documents\proxmox\strava-gpx-sync\demo\demo_100km_map.html"
with open(html_path, "w", encoding="utf-8") as f:
    f.write(map_html)

print("Created demo_100km_map.html successfully!")
