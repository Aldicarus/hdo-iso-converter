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


async def _extract_rpu_from_mkv(mkv: Path, tmp_hevc: Path, dest_rpu: Path) -> bool:
    """Re-extrae el RPU de un MKV CMv4.0 (caso target_rpu_source='mkv').
    Coste: 30-90s por MKV. Best-effort."""
    try:
        # ffmpeg -i MKV -map 0:v:0 -c:v copy -bsf:v hevc_mp4toannexb -f hevc tmp.hevc
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(mkv),
            "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc", str(tmp_hevc),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0 or not tmp_hevc.exists():
            return False
        # dovi_tool extract-rpu tmp.hevc -o dest.bin
        proc = await asyncio.create_subprocess_exec(
            "dovi_tool", "extract-rpu", str(tmp_hevc), "-o", str(dest_rpu),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=300)
        return proc.returncode == 0 and dest_rpu.exists() and dest_rpu.stat().st_size > 0
    except Exception as e:
        print(f"      ⚠ mkv re-extract failed: {e}", file=sys.stderr)
        return False


async def _dump_l8_combos_detail(result: dict, workdir_base: Path | None,
                                 redownload: bool) -> None:
    """Imprime TODOS los combos L8 únicos del bin con sus valores delta
    respecto a 2048. Útil para discernir 'master real minimal' vs
    'sintético con jitter' en casos indeterminate."""
    import json as _json
    session_id = result.get("session_id", "")
    name = result.get("name", "?")
    print(f"\n  ── {name}  ({session_id})")

    # Localizar el bin
    bin_path = None
    config_dir = _find_first_existing(CONFIG_DIR_CANDIDATES)
    if config_dir:
        try:
            session_data = _json.loads((config_dir / f"{session_id}.json").read_text(encoding="utf-8"))
        except Exception:
            session_data = {}
    else:
        session_data = {}

    # Workdir
    if workdir_base and session_id:
        candidate = workdir_base / session_id / "RPU_target.bin"
        if candidate.exists():
            bin_path = candidate

    # path local
    if bin_path is None:
        rpu_src = session_data.get("target_rpu_source", "")
        rpu_path_str = session_data.get("target_rpu_path", "")
        if rpu_src == "path" and rpu_path_str and Path(rpu_path_str).exists():
            bin_path = Path(rpu_path_str)

    # Re-descarga del Drive
    needs_cleanup = False
    if bin_path is None and redownload:
        file_id = session_data.get("pending_target_file_id", "")
        if not file_id:
            rpu_path_str = session_data.get("target_rpu_path", "")
            if rpu_path_str.startswith("drive://"):
                file_id = rpu_path_str[len("drive://"):].split("/", 1)[0]
        if file_id:
            tmp_path = Path(tempfile.gettempdir()) / f"detail_{session_id}.bin"
            ok = await _download_bin_from_drive(file_id, tmp_path)
            if ok:
                bin_path = tmp_path
                needs_cleanup = True

    if bin_path is None or not bin_path.exists():
        print(f"     ✗ No se puede inspeccionar — añade --redownload o asegúrate del workdir.")
        return

    # Export + parseo de TODOS los combos L8
    tmp_export = Path(tempfile.gettempdir()) / f"detail_export_{session_id}.json"
    try:
        from phases.rpu_analyze import _run_export, _parse_export
        rc, stderr = await _run_export(bin_path, tmp_export)
        if rc != 0 or not tmp_export.exists():
            print(f"     ✗ export falló: {stderr[:200]}")
            return
        analysis = await asyncio.to_thread(_parse_export, tmp_export)

        def hl(v):
            if v is None: return "-"
            if v == 2048: return "2048"
            d = v - 2048
            return f"{v}({d:+d})"

        print(f"     L8 combos únicos: {len(analysis.l8_combos)}  · "
              f"scene cuts: {analysis.scene_cuts}  · "
              f"frames cmv40: {analysis.frames_with_cmv40}")
        # Listar todos los combos en orden de frecuencia
        for c in analysis.l8_combos:
            print(f"       count={c.occurrence_count:>7}  "
                  f"idx={c.target_display_index} "
                  f"slope={hl(c.trim_slope)}  "
                  f"off={hl(c.trim_offset)}  "
                  f"pow={hl(c.trim_power)}  "
                  f"chr={hl(c.trim_chroma_weight)}  "
                  f"sat={hl(c.trim_saturation_gain)}  "
                  f"msw={hl(c.ms_weight)}  "
                  f"mid_c={c.target_mid_contrast}  clip={c.clip_trim}")

        # Veredicto rápido: ¿hay combos con delta significativo (>50) en
        # algún campo? Si todos están a delta ≤ 5 → sintético con jitter.
        # Si alguno tiene delta significativo → real minimal.
        significant_combos = sum(
            1 for c in analysis.l8_combos
            if abs((c.trim_slope or 2048) - 2048) > 50
            or abs((c.trim_offset or 2048) - 2048) > 50
            or abs((c.trim_power or 2048) - 2048) > 50
            or abs((c.trim_saturation_gain or 2048) - 2048) > 50
        )
        verdict = (
            "🟢 PROBABLE REAL minimal — al menos un combo tiene trim significativo"
            if significant_combos > 0
            else "🔴 PROBABLE SINTÉTICO — todos los combos están a ≤50 unidades del neutro"
        )
        print(f"     Veredicto: {verdict}")
    finally:
        try:
            tmp_export.unlink(missing_ok=True)
        except OSError:
            pass
        if needs_cleanup:
            try:
                bin_path.unlink(missing_ok=True)
            except OSError:
                pass


def _skip_reason(data: dict, redownload: bool) -> str:
    """Devuelve un código de motivo legible para saber POR QUÉ se saltó
    una sesión. Útil para diferenciar entre 'no bin info' vs 'bin info
    pero descarga falló'."""
    rpu_src = data.get("target_rpu_source", "")
    rpu_path = data.get("target_rpu_path", "")
    has_pending = bool(data.get("pending_target_file_id"))
    if not rpu_src and not has_pending:
        return "skip:no-bin-info"
    if rpu_src == "drive" or has_pending:
        if not redownload:
            return "skip:need-redownload"
        return "skip:drive-fail"
    if rpu_src == "path":
        return "skip:path-gone" if rpu_path else "skip:no-path"
    if rpu_src == "mkv":
        return "skip:mkv-gone" if rpu_path else "skip:no-mkv"
    return f"skip:{rpu_src}" if rpu_src else "skip"


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
        "output_mkv_name": data.get("output_mkv_name", ""),
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
        "suggested_filename": "",
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

        # Opción 3: usar el bin local si la sesión apuntaba a una ruta
        # (target_rpu_source="path") y el fichero aún existe en disco.
        if bin_path is None:
            rpu_src = data.get("target_rpu_source", "")
            rpu_path_str = data.get("target_rpu_path", "")
            if rpu_src == "path" and rpu_path_str and Path(rpu_path_str).exists():
                bin_path = Path(rpu_path_str)
                res["bin_origin"] = "path-cached"

        # Opción 4: re-descargar del Drive. Buscamos el file_id en dos sitios:
        #   - pending_target_file_id (sesiones post-Bloque pending_target)
        #   - target_rpu_path con formato "drive://<FILE_ID>/<FILE_NAME>"
        #     (sesiones antiguas — populadas por preflight_target_drive)
        if bin_path is None and redownload:
            file_id = data.get("pending_target_file_id", "")
            if not file_id:
                rpu_src = data.get("target_rpu_source", "")
                rpu_path_str = data.get("target_rpu_path", "")
                if rpu_src == "drive" and rpu_path_str.startswith("drive://"):
                    after = rpu_path_str[len("drive://"):]
                    file_id = after.split("/", 1)[0]
            if file_id:
                tmp_path = Path(tempfile.gettempdir()) / f"audit_bin_{session_id}.bin"
                ok = await _download_bin_from_drive(file_id, tmp_path)
                if ok:
                    bin_path = tmp_path
                    res["bin_origin"] = "redownload"

        # Opción 5: el target vino de extraer el RPU de otro MKV
        # (target_rpu_source="mkv"). Podemos re-extraer si el MKV aún
        # existe. Caro pero opcional con --redownload.
        if bin_path is None and redownload:
            rpu_src = data.get("target_rpu_source", "")
            mkv_path_str = data.get("target_rpu_path", "")
            if rpu_src == "mkv" and mkv_path_str and Path(mkv_path_str).exists():
                # Hacemos extract-rpu sobre el MKV original. Coste: 30-90s.
                tmp_rpu = Path(tempfile.gettempdir()) / f"audit_bin_{session_id}.bin"
                tmp_hevc = Path(tempfile.gettempdir()) / f"audit_hevc_{session_id}.hevc"
                ok = await _extract_rpu_from_mkv(Path(mkv_path_str), tmp_hevc, tmp_rpu)
                if ok:
                    bin_path = tmp_rpu
                    res["bin_origin"] = "mkv-reextract"
                # Borrar HEVC intermedio (no necesario)
                try:
                    tmp_hevc.unlink(missing_ok=True)
                except OSError:
                    pass

        if bin_path is None:
            res["bin_origin"] = _skip_reason(data, redownload)
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

        # Limpiar bin temporal si lo descargamos o re-extrajimos
        if res["bin_origin"] in ("redownload", "mkv-reextract") and bin_path.exists():
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

    # Calcular el filename que el MKV debería tener según el tier detectado.
    # Solo si:
    #   - el proyecto está done (hay MKV procesado)
    #   - no es un caso "should be keep" (esos requieren acción distinta)
    #   - el output_mkv_name actual NO contiene ya el label correcto
    res["suggested_filename"] = _suggest_filename(
        data.get("output_mkv_name", ""), label, classification, actually_processed,
    )

    return res


def _suggest_filename(current: str, label: str, classification: str,
                      actually_processed: bool) -> str:
    """Devuelve el filename que el MKV debería tener según la calidad
    detectada. Cadena vacía si no aplica (proyecto no procesado, ya está
    correcto, o caso 'should be keep' que no admite simple rename).
    """
    import re
    if not actually_processed or not current:
        return ""
    if classification == "default":
        # Si era sintético y se procesó, el rename no es la solución —
        # el MKV en sí debería no existir. No sugerimos rename.
        return ""
    if not label:
        return ""
    # Si el current ya contiene el label correcto → sin cambio
    expected_token = f"[{label}]"
    if expected_token in current:
        return ""
    # Sustituir [CMv4.0] o [CMv4 XXX] viejo por el correcto
    pattern = r"\[CMv4(?:\.0| (?:CORE\+?|FULL|MINIMAL))\]"
    if re.search(pattern, current):
        return re.sub(pattern, expected_token, current)
    # No tiene bracket reconocible — el usuario habrá renombrado a mano,
    # no tocamos.
    return ""


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
    parser.add_argument("--detail", type=str, default=None,
                        help="Patrón (substring) para mostrar todos los combos L8 con sus "
                             "valores delta-respecto-a-2048 de las sesiones que matcheen. "
                             "Útil para los casos 'indeterminate' donde hace falta decidir "
                             "manualmente si el bin es real o sintético.")
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

    detail_pattern = (args.detail or "").lower()

    # ── Modo --detail: solo inspeccionar matches, saltar análisis completo ──
    # Cuando el usuario pasa --detail, asumimos que ya corrió la auditoría
    # general antes y solo quiere el detalle de unos pocos casos. Evita
    # re-descargar los 60+ bins en cada inspección.
    if detail_pattern:
        print(f"# Modo --detail (substring: {args.detail!r}) — saltando análisis general")
        print()
        # Filtrar sesiones cuyo filename o contenido (source_mkv_name) contenga el pattern
        import json as _json
        matched_files = []
        for sf in session_files:
            try:
                data = _json.loads(sf.read_text(encoding="utf-8"))
                name = (data.get("source_mkv_name") or "").lower()
                if (detail_pattern in name) or (detail_pattern in sf.stem.lower()):
                    matched_files.append((sf, data))
            except Exception:
                pass
        print(f"# Sesiones matcheadas: {len(matched_files)}")
        print()
        print("=" * 100)
        print(f"  INSPECCIÓN DETALLADA — pattern: {args.detail!r}")
        print("=" * 100)
        if not matched_files:
            print(f"\n  ✗ Ningún proyecto matchea el patrón.")
            return 0
        for sf, data in matched_files:
            # Construir un "result" mínimo para que _dump_l8_combos_detail
            # pueda hacer su trabajo (necesita session_id + name).
            r = {
                "session_id": sf.stem,
                "name": data.get("source_mkv_name") or sf.stem,
            }
            await _dump_l8_combos_detail(r, workdir_base, args.redownload)
        return 0

    # ── Análisis completo (default) ─────────────────────────────────────
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

    header = ("name", "src_wf", "calidad", "L8 combos", "L8 trim%", "scene cuts",
              "origin", "phase", "wf_out", "desperdicio?")
    print(f"  {header[0]:<60} {header[1]:<8} {header[2]:<14} "
          f"{header[3]:>10} {header[4]:>9} {header[5]:>11} "
          f"{header[6]:<12} {header[7]:<8} {header[8]:<16} {header[9]}")
    print(f"  {'-' * 60} {'-' * 8} {'-' * 14} {'-' * 10} {'-' * 9} {'-' * 11} "
          f"{'-' * 12} {'-' * 8} {'-' * 16} {'-' * 14}")

    for r in filtered:
        name = (r.get("name") or "")[:58]
        trim_pct = (1.0 - r.get("l8_neutral_pct", 0.0)) * 100
        waste = "  ⚠ SÍ" if r.get("should_have_been_keep") else ""
        # Mostrar el label final que iría al filename (CMv4 CORE / CORE+ /
        # FULL), o el motivo si no aplica (sintético / skip).
        calidad = r.get("label") or (r.get("tier") or "?")
        print(f"  {name:<60} "
              f"{r.get('source_workflow','')[:8]:<8} "
              f"{calidad[:14]:<14} "
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

    # ── Renombrados sugeridos ──────────────────────────────────────────
    rename_candidates = [r for r in results if r.get("suggested_filename")]
    if rename_candidates:
        print()
        print("=" * 100)
        print(f"  RENOMBRADOS SUGERIDOS ({len(rename_candidates)} ficheros)")
        print("  Los MKVs ya procesados no llevan el label de calidad correcto en su nombre.")
        print("  Puedes renombrarlos manualmente con `mv` desde /mnt/output.")
        print("=" * 100)
        print()
        # Mostramos cada par actual → sugerido en formato copy-paste friendly
        for r in rename_candidates:
            actual = r.get("output_mkv_name", "")
            sugerido = r.get("suggested_filename", "")
            calidad = r.get("label", "")
            print(f"  📁 {actual}")
            print(f"     → {sugerido}   ({calidad})")
            print()
        # Sugerencia: bloque de comandos mv para copiar-pegar
        print("  Bloque de comandos mv (revisa antes de ejecutar):")
        for r in rename_candidates:
            actual = r.get("output_mkv_name", "").replace('"', '\\"')
            sugerido = r.get("suggested_filename", "").replace('"', '\\"')
            print(f'    mv "/mnt/output/{actual}" "/mnt/output/{sugerido}"')

    # ── Sesiones desperdiciadas (Should-be-Keep procesadas) ────────────
    wasted = [r for r in results if r.get("should_have_been_keep")]
    if wasted:
        print()
        print("=" * 100)
        print(f"  ⚠️ SESIONES DESPERDICIADAS ({len(wasted)})")
        print("  Estos MKVs se procesaron pero su bin era sintético — el resultado visible es")
        print("  equivalente a la conversión al vuelo del reproductor. Considera borrarlos y")
        print("  mantener el MKV original en su lugar.")
        print("=" * 100)
        print()
        for r in wasted:
            print(f"  ⚠ {r.get('output_mkv_name') or r.get('name')}")
            print(f"     bin sintético — L8 {r.get('l8_unique_count')} combos, "
                  f"{(1.0 - r.get('l8_neutral_pct', 0.0)) * 100:.0f}% trim")

    # ── Volcar CSV opcional ────────────────────────────────────────────
    if args.csv:
        cols = ["session_id", "name", "output_mkv_name", "source_workflow", "target_type",
                "phase", "output_workflow", "bin_origin", "tier", "label",
                "suggested_filename",
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
