"""Auditoría retroactiva de los bins usados en proyectos CMv4.0 históricos.

Analiza TODAS las sesiones CMv4.0 persistidas y reporta:
  1. Cuáles bins eran sintéticos (recommendation real = Keep) — trabajo
     desperdiciado en jobs ya ejecutados.
  2. Distribución empírica de la calidad de los bins (CORE / CORE+ / FULL)
     para calibrar los umbrales del classifier.
  3. Estadísticas globales (min/median/max/p95 de combos L8, neutral_pct,
     scene_cuts) para decidir si los umbrales actuales son correctos.

USO

  Dentro del contenedor Docker:
    docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins

  Opciones:
    --redownload   Descarga el bin del Drive si no hay análisis previo ni
                   workdir local. Requiere Google API key configurada.
    --csv <path>   Vuelca el resultado a CSV.
    --filter-keep  Solo muestra las sesiones donde la recomendación
                   retroactiva es "keep" (los desperdiciados).

ESTRATEGIA DE ANÁLISIS

Para cada sesión:
  1. Si tiene `target_l8_unique_count > 0` ya persistido (Bloque 1 ejecutó
     pre-flight enriquecido) → usa esos datos.
  2. Si no, busca `RPU_target.bin` en el workdir (`/mnt/tmp/cmv40/{id}/`).
     Si existe → ejecuta análisis ahora.
  3. Si no existe y --redownload + `pending_target_kind=="drive"` con
     `pending_target_file_id` set → descarga el bin a /tmp y analiza.
  4. Si nada de lo anterior funciona → marca como "no analizable".

Solo lectura — no modifica sesiones ni dispara fases.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

# Asegurar imports relativos cuando se ejecuta como módulo
sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.rpu_analyze import (  # noqa: E402
    analyze_rpu_combos,
    classify_l8,
    classify_l8_quality,
    RpuAnalysis,
)


# Detección del directorio config en el contenedor o local
CONFIG_DIR_CANDIDATES = [
    Path("/config/cmv40"),
    Path("/share/Media/Apps/HDOTools/hdo-config/cmv40"),
    Path(__file__).parent.parent.parent / "local_data" / "config" / "cmv40",
]
WORKDIR_BASE_CANDIDATES = [
    Path("/mnt/tmp/cmv40"),
    Path("/share/ZFS20_DATA/Container/tmp/cmv40"),
]


def _find_first_existing(candidates: list[Path]) -> Path | None:
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    return None


async def _download_bin_from_drive(file_id: str, dest: Path) -> bool:
    """Best-effort: intenta descargar el bin del Drive si el script corre
    dentro de un entorno con httpx + Google API key configurada."""
    try:
        from services.rec999_drive import download_file
        await download_file(file_id, dest)
        return True
    except Exception as e:
        print(f"      ⚠ download failed: {e}", file=sys.stderr)
        return False


async def _analyze_session(
    session_path: Path,
    workdir_base: Path | None,
    redownload: bool,
) -> dict:
    """Devuelve un dict con el análisis del bin de una sesión."""
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"cannot parse json: {e}"}

    name = data.get("source_mkv_name") or data.get("id", "?")
    phase = data.get("phase", "?")
    output_workflow = data.get("output_workflow", "")

    res = {
        "name": name,
        "phase": phase,
        "output_workflow": output_workflow,
        "source_workflow": data.get("source_workflow", ""),
        "target_type": data.get("target_type", ""),
        "target_preflight_ok": data.get("target_preflight_ok", False),
        "preflight_decision": data.get("preflight_decision", ""),
        "recommended_action_persisted": data.get("recommended_action", ""),
        "bin_origin": "",   # "persisted" | "workdir" | "redownload" | "skip"
        "l8_unique_count": 0,
        "l8_neutral_pct": 0.0,
        "l8_has_mid_contrast": False,
        "l8_has_clip_trim": False,
        "scene_cuts": 0,
        "l2_unique_count": 0,
        "tier": "",
        "label": "",
        "should_have_been_keep": False,
    }

    # Opción 1: análisis ya persistido (Bloque 1+)
    if (data.get("target_l8_unique_count") or 0) > 0:
        res["bin_origin"] = "persisted"
        res["l8_unique_count"] = data.get("target_l8_unique_count", 0)
        res["l8_neutral_pct"] = data.get("target_l8_neutral_frames_pct", 0.0)
        res["l8_has_mid_contrast"] = data.get("target_l8_has_mid_contrast", False)
        res["l8_has_clip_trim"] = data.get("target_l8_has_clip_trim", False)
        res["scene_cuts"] = data.get("target_l8_scene_cuts", 0)
        res["l2_unique_count"] = data.get("target_l2_unique_count", 0)
        # Reconstruir RpuAnalysis para reclassify (asegura coherencia con
        # los umbrales actuales aunque la session se persistió con otros).
        a = RpuAnalysis()
        a.l8_unique_count = res["l8_unique_count"]
        a.l8_neutral_pct = res["l8_neutral_pct"]
        a.l8_has_mid_contrast = res["l8_has_mid_contrast"]
        a.l8_has_clip_trim = res["l8_has_clip_trim"]
        a.scene_cuts = res["scene_cuts"]
        a.l2_unique_count = res["l2_unique_count"]
        a.frames_with_cmv40 = data.get("target_frames_analyzed", 0)
        a.total_frames = data.get("target_frames_analyzed", 0)
    else:
        # Opción 2: workdir local con RPU_target.bin
        bin_path = None
        session_id = data.get("id", "")
        if workdir_base and session_id:
            candidate = workdir_base / session_id / "RPU_target.bin"
            if candidate.exists():
                bin_path = candidate
                res["bin_origin"] = "workdir"

        # Opción 3: re-descargar del Drive
        if bin_path is None and redownload:
            file_id = data.get("pending_target_file_id", "")
            if file_id:
                tmp_path = Path(tempfile.gettempdir()) / f"audit_bin_{session_id}.bin"
                ok = await _download_bin_from_drive(file_id, tmp_path)
                if ok:
                    bin_path = tmp_path
                    res["bin_origin"] = "redownload"

        if bin_path is None:
            res["bin_origin"] = "skip"
            return res

        # Analizar bin
        a = await analyze_rpu_combos(bin_path)
        if a.total_frames == 0:
            res["bin_origin"] = f"{res['bin_origin']}-failed"
            return res
        res["l8_unique_count"] = a.l8_unique_count
        res["l8_neutral_pct"] = a.l8_neutral_pct
        res["l8_has_mid_contrast"] = a.l8_has_mid_contrast
        res["l8_has_clip_trim"] = a.l8_has_clip_trim
        res["scene_cuts"] = a.scene_cuts
        res["l2_unique_count"] = a.l2_unique_count

        # Limpiar bin temporal si lo descargamos
        if res["bin_origin"] == "redownload" and bin_path.exists():
            try:
                bin_path.unlink()
            except OSError:
                pass

    classification, _ = classify_l8(a)
    tier, label, _ = classify_l8_quality(a)
    res["classification"] = classification
    res["tier"] = tier or classification
    res["label"] = label or ("(sintético)" if classification == "default" else "(ambiguo)")

    # ¿La sesión debería haber sido Keep? El usuario procesó este job,
    # marcar si nuestro modelo retroactivo dice que fue desperdiciado.
    actually_processed = phase == "done" and output_workflow in ("", "restore_dropin", "restore_merge")
    if actually_processed and classification == "default":
        res["should_have_been_keep"] = True

    return res


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--redownload", action="store_true",
                        help="Re-descarga bins del Drive si no hay análisis previo ni workdir.")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Vuelca el resultado a CSV.")
    parser.add_argument("--filter-keep", action="store_true",
                        help="Solo muestra sesiones desperdiciadas (procesadas pero deberían haber sido Keep).")
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="Override del directorio de sesiones (default: autodetectado).")
    parser.add_argument("--workdir-base", type=Path, default=None,
                        help="Override del workdir base de cmv40 (default: autodetectado).")
    args = parser.parse_args()

    config_dir = args.config_dir or _find_first_existing(CONFIG_DIR_CANDIDATES)
    if not config_dir:
        print(f"✗ No se encontró config dir. Probados: {CONFIG_DIR_CANDIDATES}", file=sys.stderr)
        return 1
    workdir_base = args.workdir_base or _find_first_existing(WORKDIR_BASE_CANDIDATES)

    print(f"# Auditoría de bins CMv4.0")
    print(f"# Config dir: {config_dir}")
    print(f"# Workdir base: {workdir_base or '(no encontrado)'}")
    print(f"# Re-download del Drive: {'sí' if args.redownload else 'no'}")
    print()

    session_files = sorted(config_dir.glob("cmv40_*.json"))
    print(f"# Sesiones a analizar: {len(session_files)}")
    print()

    results = []
    for i, sf in enumerate(session_files, 1):
        print(f"  [{i}/{len(session_files)}] {sf.name}", flush=True)
        try:
            r = await _analyze_session(sf, workdir_base, args.redownload)
            r["session_id"] = sf.stem
            results.append(r)
        except Exception as e:
            print(f"      ⚠ error: {e}", file=sys.stderr)

    # ── Reporte tabular ────────────────────────────────────────────────
    print()
    print("=" * 100)
    if args.filter_keep:
        filtered = [r for r in results if r.get("should_have_been_keep")]
        print(f"  SESIONES DESPERDICIADAS (procesadas pero deberían haber sido Keep): {len(filtered)}")
    else:
        filtered = results
        print(f"  ANÁLISIS COMPLETO ({len(results)} sesiones)")
    print("=" * 100)
    print()

    header = ("name", "src_wf", "tier", "L8 combos", "L8 trim%", "scene cuts",
              "origin", "phase", "wf_out", "desperdicio?")
    print(f"  {header[0]:<60} {header[1]:<8} {header[2]:<10} "
          f"{header[3]:>10} {header[4]:>9} {header[5]:>11} "
          f"{header[6]:<12} {header[7]:<8} {header[8]:<16} {header[9]}")
    print(f"  {'-' * 60} {'-' * 8} {'-' * 10} {'-' * 10} {'-' * 9} {'-' * 11} "
          f"{'-' * 12} {'-' * 8} {'-' * 16} {'-' * 14}")

    for r in filtered:
        name = (r.get("name") or "")[:58]
        trim_pct = (1.0 - r.get("l8_neutral_pct", 0.0)) * 100
        waste = "  ⚠ SÍ" if r.get("should_have_been_keep") else ""
        print(f"  {name:<60} "
              f"{r.get('source_workflow','')[:8]:<8} "
              f"{(r.get('tier') or '?')[:10]:<10} "
              f"{r.get('l8_unique_count', 0):>10} "
              f"{trim_pct:>8.1f}% "
              f"{r.get('scene_cuts', 0):>11} "
              f"{r.get('bin_origin', ''):<12} "
              f"{r.get('phase', '')[:8]:<8} "
              f"{r.get('output_workflow', '')[:16]:<16} "
              f"{waste}")

    # ── Estadísticas globales para calibración ────────────────────────
    print()
    print("=" * 100)
    print("  ESTADÍSTICAS PARA CALIBRACIÓN")
    print("=" * 100)

    analyzed = [r for r in results if r.get("l8_unique_count", 0) > 0 or r.get("classification") == "default"]
    tier_counts = Counter(r.get("tier", "?") for r in analyzed)
    bin_origin_counts = Counter(r.get("bin_origin", "?") for r in results)
    waste_count = sum(1 for r in results if r.get("should_have_been_keep"))

    print()
    print(f"  Sesiones totales: {len(results)}")
    print(f"  Analizadas:       {len(analyzed)}")
    print(f"  Sin análisis:     {len(results) - len(analyzed)}")
    print()
    print(f"  Origen del análisis:")
    for origin, cnt in bin_origin_counts.most_common():
        print(f"    {origin:<16}  {cnt:>4}")
    print()
    print(f"  Distribución por tier:")
    for tier, cnt in tier_counts.most_common():
        pct = 100 * cnt / max(1, len(analyzed))
        print(f"    {tier:<14}  {cnt:>4}  ({pct:.1f}%)")
    print()
    print(f"  💸 Sesiones DESPERDICIADAS (procesadas pero deberían ser Keep): {waste_count}")
    if waste_count > 0:
        wasted_time_min = waste_count * 25  # ~25 min por job
        print(f"     ≈ {wasted_time_min} min ({wasted_time_min // 60}h {wasted_time_min % 60}min) de procesado innecesario")

    # Estadísticas de los bins reales (para calibrar)
    real_bins = [r for r in analyzed if r.get("classification") == "real"]
    default_bins = [r for r in analyzed if r.get("classification") == "default"]

    if real_bins:
        combos = sorted(r["l8_unique_count"] for r in real_bins)
        worked = sorted((1.0 - r["l8_neutral_pct"]) * 100 for r in real_bins)
        print()
        print(f"  Bins REALES ({len(real_bins)}) — distribución para calibrar umbrales:")
        if len(combos) >= 4:
            print(f"    L8 combos únicos:  min={combos[0]}  p25={combos[len(combos)//4]}  "
                  f"median={statistics.median(combos):.0f}  "
                  f"p75={combos[3 * len(combos)//4]}  max={combos[-1]}")
            print(f"    % frames con trim: min={worked[0]:.1f}  "
                  f"median={statistics.median(worked):.1f}  "
                  f"max={worked[-1]:.1f}")
        else:
            print(f"    L8 combos: {combos}")
            print(f"    % trim:    {[round(w, 1) for w in worked]}")

    if default_bins:
        combos = sorted(r["l8_unique_count"] for r in default_bins)
        worked = sorted((1.0 - r["l8_neutral_pct"]) * 100 for r in default_bins)
        print()
        print(f"  Bins DEFAULT ({len(default_bins)}) — para validar que el detector no es demasiado agresivo:")
        if len(combos) >= 4:
            print(f"    L8 combos únicos:  min={combos[0]}  median={statistics.median(combos):.0f}  max={combos[-1]}")
            print(f"    % frames con trim: min={worked[0]:.1f}  median={statistics.median(worked):.1f}  max={worked[-1]:.1f}")
        else:
            print(f"    L8 combos: {combos}")
            print(f"    % trim:    {[round(w, 1) for w in worked]}")

    # ── Volcar CSV opcional ────────────────────────────────────────────
    if args.csv:
        cols = ["session_id", "name", "source_workflow", "target_type",
                "phase", "output_workflow", "bin_origin", "tier", "label",
                "l8_unique_count", "l8_neutral_pct", "l8_has_mid_contrast",
                "l8_has_clip_trim", "scene_cuts", "l2_unique_count",
                "should_have_been_keep", "preflight_decision",
                "recommended_action_persisted"]
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"\n  📄 CSV escrito: {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
