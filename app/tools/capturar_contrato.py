"""Captura las salidas REALES de las herramientas para los tests de contrato.

Se ejecuta DENTRO de un contenedor de la imagen cuyas herramientas se quieren
fijar, sobre un MKV real y tres RPU reales —el de un disco (P7 CMv2.9), un bin
CMv4.0 y uno ya convertido a Profile 8.1—, y escribe un JSON por stdout. Solo
lee: el export por niveles va a un temporal que se borra al terminar.

    docker cp app/tools/capturar_contrato.py hdo-iso-converter:/tmp/
    docker exec hdo-iso-converter python3 /tmp/capturar_contrato.py \\
        "/mnt/library/…/Peli.mkv" /mnt/tmp/cmv40/<proyecto> > capturado.json

Los RPU salen del workdir de un proyecto CMv4.0 (`RPU_source.bin`,
`RPU_target.bin`, `RPU_merged_p81.bin`) o se dan uno a uno, que es lo que hace
falta cuando el workdir ya se limpió:

    … capturar_contrato.py Peli.mkv --rpu RPU_source.bin=/ruta/a.bin \\
        --rpu RPU_target.bin=/ruta/b.bin --rpu RPU_merged_p81.bin=/ruta/c.bin

Con `--fixtures DIR` escribe además `app/tests/fixtures_contrato/` ya
recortado (el `codec_private_data` a 16 caracteres y el SegmentUID
anonimizado, que lleva la marca de procedencia, en mkvmerge y en el
`UniqueID` de MediaInfo, que es el mismo en decimal) y conserva de las fixtures
que encuentre lo que esta captura no repite (las salidas de ffprobe sobre
otros MKV). De ahí lee `test_contrato_de_los_fakes`. Repetirla cuando cambie
la versión de una herramienta en el Dockerfile: el contrato es con ESA
versión.
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

NIVELES = ["level1", "level2", "level3", "level5", "level6", "level8", "level9", "level11"]
NOMBRES_RPU = ("RPU_source.bin", "RPU_target.bin", "RPU_merged_p81.bin")
#: El SegmentUID de un MKV de la app es su marca de procedencia (`firma.py`):
#: en una fixture del repositorio va anonimizado. Es 00 11 22 … ff, el que ya
#: llevaba la primera captura; se compone en vez de escribirse porque 32
#: dígitos hexadecimales seguidos son la forma de una clave, y el guard de
#: `test_clave_tmdb_de_la_app` los busca en todo el repositorio.
UID_ANONIMO = bytes(range(0, 256, 17)).hex()


def run(cmd, timeout=600):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout)
    return {"argv": [Path(str(a)).name if str(a).startswith("/") else str(a) for a in cmd[1:]],
            "rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}


def capturar(mkv: str, rpus: dict, tmp_base: str) -> dict:
    out = {"versiones": {}, "salidas": {}}
    for b, a in (("dovi_tool", "--version"), ("mkvmerge", "--version"),
                 ("mediainfo", "--version"), ("ffmpeg", "-version"), ("ffprobe", "-version")):
        out["versiones"][b] = run([b, a])["stdout"].strip().splitlines()[-1 if b == "mediainfo" else 0]

    S = out["salidas"]
    for nombre in NOMBRES_RPU:
        S[f"dovi_summary:{nombre}"] = run(["dovi_tool", "info", "-i", rpus[nombre], "--summary"])

    tmp = Path(tempfile.mkdtemp(dir=tmp_base))
    r = run(["dovi_tool", "export", "-i", rpus["RPU_target.bin"], "-f", "json",
             "-d", f"scenes={tmp}/scenes.txt",
             "--levels", ",".join(f"{lv}={tmp}/{lv}.json" for lv in NIVELES)], timeout=1500)
    r["ficheros"] = {}
    for lv in NIVELES:
        p = tmp / f"{lv}.json"
        if p.exists():
            d = json.loads(p.read_text())
            r["ficheros"][lv] = {"tipo": type(d).__name__, "n": len(d),
                                 "primeros": d[:4] if isinstance(d, list) else d}
    sc = tmp / "scenes.txt"
    if sc.exists():
        lineas = sc.read_text().splitlines()
        r["ficheros"]["scenes"] = {"n": len(lineas), "primeras": lineas[:6]}
    S["dovi_export_levels"] = r
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()

    S["mkvmerge_J"] = run(["mkvmerge", "-J", mkv])
    S["mediainfo_json"] = run(["mediainfo", "--Output=JSON", mkv])
    S["ffprobe_nb_frames"] = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=nb_frames", "-of", "csv=p=0", mkv])
    S["ffprobe_duration"] = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", mkv])
    S["ffmpeg_progreso"] = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", mkv, "-map", "0:v:0",
                                "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-t", "4",
                                "-f", "hevc", "-y", "/dev/null"])
    return out


def _recortar_j(j: dict) -> dict:
    for t in j.get("tracks", []):
        p = t.get("properties") or {}
        if p.get("codec_private_data"):
            p["codec_private_data"] = p["codec_private_data"][:16]
    props = (j.get("container") or {}).get("properties") or {}
    if "segment_uid" in props:
        props["segment_uid"] = UID_ANONIMO
    return j


def _recortar_mi(mi: dict) -> dict:
    """El `UniqueID` de la pista General de MediaInfo es el MISMO SegmentUID,
    en decimal: sin esto la fixture de MediaInfo deshacía lo que anonimiza
    `_recortar_j` (pasó desde la primera captura hasta la del 2026-10-28)."""
    for t in (mi.get("media") or {}).get("track") or []:
        if t.get("@type") == "General" and "UniqueID" in t:
            t["UniqueID"] = str(int(UID_ANONIMO, 16))
    return mi


def escribir_fixtures(c: dict, carpeta: Path, origen: str) -> None:
    S = c["salidas"]
    carpeta.mkdir(parents=True, exist_ok=True)
    f_reales = carpeta / "herramientas_reales.json"
    reales = json.loads(f_reales.read_text(encoding="utf-8")) if f_reales.exists() else {}
    reales["_origen"] = origen
    reales["versiones"] = c["versiones"]
    reales["dovi_summary"] = {n: S[f"dovi_summary:{n}"]["stdout"] for n in NOMBRES_RPU}
    reales["dovi_export_levels"] = {"stdout": S["dovi_export_levels"]["stdout"],
                                    "ficheros": S["dovi_export_levels"]["ficheros"]}
    reales["ffprobe_nb_frames_csv"] = S["ffprobe_nb_frames"]["stdout"]
    reales["ffprobe_duration_csv"] = S["ffprobe_duration"]["stdout"]
    reales["ffmpeg_lineas_de_progreso"] = [
        l.strip() for l in S["ffmpeg_progreso"]["stderr"].replace("\r", "\n").splitlines()
        if "Lsize=" in l][-1:]
    f_reales.write_text(json.dumps(reales, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (carpeta / "mkvmerge_J_mkv.json").write_text(
        json.dumps(_recortar_j(json.loads(S["mkvmerge_J"]["stdout"])), ensure_ascii=False,
                   indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (carpeta / "mediainfo_mkv.json").write_text(
        json.dumps(_recortar_mi(json.loads(S["mediainfo_json"]["stdout"])), ensure_ascii=False,
                   indent=1) + "\n",
        encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mkv")
    ap.add_argument("workdir", nargs="?")
    ap.add_argument("--rpu", action="append", default=[], metavar="NOMBRE=RUTA")
    ap.add_argument("--tmp", default="/mnt/tmp")
    ap.add_argument("--fixtures")
    ap.add_argument("--origen", default="")
    args = ap.parse_args()
    rpus = {n: str(Path(args.workdir) / n) for n in NOMBRES_RPU} if args.workdir else {}
    for par in args.rpu:
        nombre, _, ruta = par.partition("=")
        rpus[nombre] = ruta
    faltan = [n for n in NOMBRES_RPU if n not in rpus]
    if faltan:
        raise SystemExit(f"faltan RPU: {faltan}")
    c = capturar(args.mkv, rpus, args.tmp)
    if args.fixtures:
        escribir_fixtures(c, Path(args.fixtures), args.origen)
    print(json.dumps(c, ensure_ascii=False))


if __name__ == "__main__":
    main()
