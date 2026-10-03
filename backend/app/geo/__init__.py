"""Audio geolocation: spoken distance + bearing -> map coordinates and zone perimeters.

Plan: README_audio_geolocation.md. Everything here is based on what the speaker says ("800 metres
north-east of me"), not on sound direction: there is no acoustic localisation.

    parse_spatial.py  Ukrainian / English compass words, numeric azimuths, clock positions, distances
    project.py        destination point and its inverse (distance and bearing between two points)
    zones.py          zone polygons, uncertainty, merging, expiry, GeoJSON
    snap.py           snap to the road network (blockages) and to drop points (destinations)
    service.py        the pipeline: event in -> zone / destination out (used by api.py)
    api.py            POST /events hook, GET/DELETE /zones, WebSocket zone messages
    zones.yaml        default radii, expiries and limits (TUNE here, not in code)
"""
