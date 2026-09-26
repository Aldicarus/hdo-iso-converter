"""Audita los bins del repo DoviTools POR TANDAS, sin agotar la cuota.

Por qué por tandas: la cuota de Drive se agota a las ~20 descargas de
60-90 MB y tarda horas en volver, así que una auditoría de golpe muere a
mitad y pierde lo hecho. Cada ejecución baja unos pocos, los mide, guarda
el resultado y BORRA el .bin — lo que interesa son los números, no el
fichero.

USO

    docker exec hdo-iso-converter python3 -m tools.auditar_bins_por_tandas
    docker exec hdo-iso-converter python3 -m tools.auditar_bins_por_tandas -n 8
    docker exec hdo-iso-converter python3 -m tools.auditar_bins_por_tandas --informe

QUÉ AUDITA, Y EN QUÉ ORDEN

  1. Los proyectos CMv4.0 terminados SIN los combos L8 guardados. Son los
     anteriores al modelo Keep/Inyectar (abr-ago 2026) y son los únicos
     donde el veredicto de hoy puede contradecir lo que se hizo.
  2. Los proyectos a medias con bin identificado.

El estado vive en /mnt/tmp (volumen, sobrevive a un `compose up --build`;
el /tmp del contenedor no) y es acumulativo: cada pasada añade, ninguna
repite lo ya medido. Solo lee las sesiones — no las modifica.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.rpu_analyze import (  # noqa: E402
    analyze_rpu_combos, classify_l8, classify_l8_quality, delta_l8_de,
)

CONFIG = Path("/config/cmv40")
BASE = Path("/mnt/tmp/auditoria_bins")
ESTADO = BASE / "estado.json"

# Una tanda pequeña por defecto: el límite de Drive no se anuncia, se
# descubre fallando, y con 5 quedan reintentos dentro de la misma hora.
POR_TANDA = 5


def leer_estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {"medidos": {}, "fallos": {}, "ultima": ""}


def guardar_estado(e: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = ESTADO.with_suffix(".tmp")
    tmp.write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ESTADO)


def file_id_de(d: dict) -> str:
    """El id de Drive del bin target, mire donde mire la sesión."""
    fid = d.get("pending_target_file_id") or ""
    if fid:
        return fid
    p = d.get("target_rpu_path") or ""
    if p.startswith("drive://"):
        return p[len("drive://"):].split("/", 1)[0]
    return ""


def nombre_de(d: dict) -> str:
    p = d.get("target_rpu_path") or ""
    if p.startswith("drive://"):
        resto = p[len("drive://"):]
        return resto.split("/", 1)[1] if "/" in resto else resto
    return Path(p).name if p else ""


def cola(estado: dict) -> list[dict]:
    """Lo que queda por medir, del más interesante al menos.

    Un proyecto terminado pesa más que uno a medias: en el terminado ya se
    gastó el pipeline, así que un veredicto distinto dice que hay un MKV
    en la biblioteca que no aporta lo que su nombre promete.
    """
    pend = []
    for f in sorted(CONFIG.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not d.get("id"):
            continue
        if d.get("target_l8_combos"):
            continue                       # ya tiene los números guardados
        fid = file_id_de(d)
        if not fid or fid in estado["medidos"]:
            continue
        pend.append({
            "id": d["id"],
            "file_id": fid,
            "bin": nombre_de(d),
            "peli": d.get("source_mkv_name") or d["id"],
            "salida": d.get("output_mkv_name") or "",
            "fase": d.get("phase") or "",
            "workflow": d.get("output_workflow") or "",
            "terminado": d.get("phase") == "done",
        })
    pend.sort(key=lambda p: (not p["terminado"], p["peli"]))
    return pend


async def medir(item: dict, estado: dict) -> str:
    """Baja un bin, lo mide y lo borra. Devuelve el veredicto o 'fallo'."""
    from services.rec999_drive import download_file
    BASE.mkdir(parents=True, exist_ok=True)
    destino = BASE / f"tmp_{item['file_id']}.bin"
    try:
        await download_file(item["file_id"], destino)
    except Exception as e:
        estado["fallos"][item["file_id"]] = {
            "peli": item["peli"], "error": str(e)[:200], "cuando": time.strftime("%F %T"),
        }
        return "fallo"
    try:
        a = await analyze_rpu_combos(destino)
        if a.total_frames == 0:
            estado["fallos"][item["file_id"]] = {
                "peli": item["peli"], "error": "el análisis no devolvió frames",
                "cuando": time.strftime("%F %T"),
            }
            return "fallo"
        clasif, motivo = classify_l8(a)
        tier, etiqueta, _ = classify_l8_quality(a)
        estado["medidos"][item["file_id"]] = {
            "peli": item["peli"], "bin": item["bin"], "salida": item["salida"],
            "fase": item["fase"], "workflow": item["workflow"],
            "clasificacion": clasif, "tier": tier, "etiqueta": etiqueta,
            "max_delta": delta_l8_de(a), "combos": a.l8_unique_count,
            "neutral_pct": round(a.l8_neutral_pct, 4), "l2": a.l2_unique_count,
            "l3": a.l3_unique_count, "scene_cuts": a.scene_cuts,
            "frames": a.frames_with_cmv40, "motivo": motivo[:300],
            "cuando": time.strftime("%F %T"),
        }
        estado["fallos"].pop(item["file_id"], None)
        return clasif
    finally:
        # El bin se borra SIEMPRE: son 60-90 MB y lo que queremos son los
        # números. Dejarlos llenaría /mnt/tmp en una semana de tandas.
        try:
            destino.unlink(missing_ok=True)
        except OSError:
            pass


def informe(estado: dict) -> None:
    m = estado["medidos"]
    print(f"\n{'=' * 74}\n  BINS MEDIDOS: {len(m)}\n{'=' * 74}")
    if not m:
        print("  (todavía ninguno)")
        return
    print("  veredictos:", dict(Counter(v["clasificacion"] for v in m.values())))
    print()
    # Lo que de verdad importa: un MKV procesado cuyo bin no aportaba.
    #
    # El corte es `fase == done` y NO `output_workflow`: ese campo lo
    # escriben los proyectos posteriores al modelo Keep/Inyectar y está
    # VACÍO en los viejos — que son justo los que este script audita. Con
    # el filtro por workflow la sección no se habría enseñado nunca, y un
    # informe que no marca nada se lee como «no hay nada que marcar».
    malos = [v for v in m.values()
             if v["clasificacion"] != "real"
             and (v["fase"] == "done"
                  or v["workflow"] in ("restore_dropin", "restore_merge"))]
    if malos:
        print(f"  ⚠ PROCESADOS con un bin que NO aportaba trims ({len(malos)}):")
        for v in malos:
            print(f"     {v['peli'][:52]:52} {v['clasificacion']:13}"
                  f" maxΔ={v['max_delta']:4} combos={v['combos']}")
        print("     (un `tone_mapping` no es un error: aporta L3/L9/L11 y el")
        print("      veredicto delega la decisión en el usuario a propósito)")
        print()
    buenos = [v for v in m.values()
              if v["clasificacion"] == "real" and v["workflow"] == "keep_cmv29"]
    if buenos:
        print(f"  ⚠ DESCARTADOS con un bin que SÍ aportaba ({len(buenos)}):")
        for v in buenos:
            print(f"     {v['peli'][:58]:58} maxΔ={v['max_delta']:4} {v['etiqueta']}")
        print()
    if estado["fallos"]:
        print(f"  fallos pendientes de reintentar: {len(estado['fallos'])}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=POR_TANDA,
                    help=f"cuántos bins bajar en esta tanda (default {POR_TANDA})")
    ap.add_argument("--informe", action="store_true",
                    help="solo enseña lo medido hasta ahora, sin descargar nada")
    ap.add_argument("--reintentar-fallos", action="store_true",
                    help="vuelve a poner en cola los que fallaron")
    args = ap.parse_args()

    estado = leer_estado()
    if args.reintentar_fallos:
        n = len(estado["fallos"])
        estado["fallos"] = {}
        guardar_estado(estado)
        print(f"  {n} fallos devueltos a la cola")

    if args.informe:
        informe(estado)
        return 0

    pend = cola(estado)
    print(f"  medidos hasta ahora : {len(estado['medidos'])}")
    print(f"  pendientes          : {len(pend)}")
    if not pend:
        print("\n  ✓ No queda ningún bin por medir.")
        informe(estado)
        return 0

    tanda = pend[:args.n]
    print(f"  esta tanda          : {len(tanda)}\n")
    cortado = False
    for i, item in enumerate(tanda, 1):
        print(f"  [{i}/{len(tanda)}] {item['peli'][:54]}", flush=True)
        r = await medir(item, estado)
        guardar_estado(estado)          # tras CADA uno: una tanda cortada
        if r == "fallo":                # por la cuota no pierde lo anterior
            err = estado["fallos"][item["file_id"]]["error"]
            print(f"          ✗ {err[:90]}")
            # Un fallo suele ser la cuota, y entonces los siguientes fallan
            # igual: se para y se deja para la próxima tanda en vez de
            # quemar los reintentos que quedan.
            cortado = True
            break
        v = estado["medidos"][item["file_id"]]
        print(f"          → {r}  maxΔ={v['max_delta']}  combos={v['combos']}"
              f"  {v['etiqueta'] or ''}")

    estado["ultima"] = time.strftime("%F %T")
    guardar_estado(estado)
    if cortado:
        print("\n  Tanda cortada tras un fallo (probablemente la cuota de Drive).")
        print("  Vuelve a lanzarla más tarde: lo medido ya está guardado.")
    quedan = len(cola(estado))
    print(f"\n  quedan {quedan} por medir.")
    informe(estado)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
