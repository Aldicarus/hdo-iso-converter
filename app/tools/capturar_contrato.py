"""Captura las salidas REALES de las herramientas para los tests de contrato.

Se ejecuta DENTRO del contenedor, sobre un MKV de Tab 3 y su workdir CMv4.0,
y escribe un JSON por stdout. Solo lee: el export por niveles va a un
temporal de /mnt/tmp que se borra al terminar.

    docker cp app/tools/capturar_contrato.py hdo-iso-converter:/tmp/
    docker exec hdo-iso-converter python3 /tmp/capturar_contrato.py \
        "/mnt/library/…/Peli.mkv" /mnt/tmp/cmv40/<proyecto> > capturado.json

De ahí salen `app/tests/fixtures_contrato/` (recortado: las primeras filas de
cada nivel, sin el `codec_private_data`) y lo que comprueba
`test_contrato_de_los_fakes`. Repetirla cuando cambie la versión de una
herramienta en el Dockerfile: el contrato es con ESA versión.
"""
import json, subprocess, sys, tempfile
from pathlib import Path

M = sys.argv[1]
W = Path(sys.argv[2])
out = {"versiones": {}, "salidas": {}}

def run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {"argv": [Path(a).name if a.startswith("/") else a for a in cmd[1:]],
            "rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}

for b, a in (("dovi_tool", "--version"), ("mkvmerge", "--version"),
             ("mediainfo", "--version"), ("ffmpeg", "-version"), ("ffprobe", "-version")):
    out["versiones"][b] = run([b, a])["stdout"].strip().splitlines()[-1 if b == "mediainfo" else 0]

S = out["salidas"]
for nombre in ("RPU_source.bin", "RPU_target.bin", "RPU_merged_p81.bin"):
    S[f"dovi_summary:{nombre}"] = run(["dovi_tool", "info", "-i", str(W / nombre), "--summary"])

tmp = Path(tempfile.mkdtemp(dir="/mnt/tmp"))
niveles = ["level1", "level2", "level3", "level5", "level6", "level8", "level9", "level11"]
r = run(["dovi_tool", "export", "-i", str(W / "RPU_target.bin"), "-f", "json",
         "-d", f"scenes={tmp}/scenes.txt",
         "--levels", ",".join(f"{lv}={tmp}/{lv}.json" for lv in niveles)], timeout=1500)
r["ficheros"] = {}
for lv in niveles:
    p = tmp / f"{lv}.json"
    if p.exists():
        d = json.loads(p.read_text())
        r["ficheros"][lv] = {"tipo": type(d).__name__, "n": len(d), "primeros": d[:4] if isinstance(d, list) else d}
sc = tmp / "scenes.txt"
if sc.exists():
    lineas = sc.read_text().splitlines()
    r["ficheros"]["scenes"] = {"n": len(lineas), "primeras": lineas[:6]}
S["dovi_export_levels"] = r
for p in tmp.iterdir():
    p.unlink()
tmp.rmdir()

S["mkvmerge_J"] = run(["mkvmerge", "-J", M])
S["mediainfo_json"] = run(["mediainfo", "--Output=JSON", M])
S["ffprobe_nb_frames"] = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_frames", "-of", "csv=p=0", M])
S["ffprobe_duration"] = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", M])
S["ffmpeg_progreso"] = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", M, "-map", "0:v:0",
                            "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-t", "4",
                            "-f", "hevc", "-y", "/dev/null"])
print(json.dumps(out, ensure_ascii=False))
