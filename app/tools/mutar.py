"""Verifica tests por mutación: aplica cada cambio, ejecuta los tests que lo
tienen que cazar y restaura el fichero.

Un test que pasa en verde no demuestra nada hasta que se le ve FALLAR con el
bug delante. La auditoría de la suite del 2026-10-17 lo midió: con 38
mutaciones aplicadas a la vez, los 3.117 tests siguieron en verde. Esta
herramienta convierte esa comprobación en un paso repetible.

Uso (desde la raíz del repo):

    python3 app/tools/mutar.py app/tools/mutaciones.json
    python3 app/tools/mutar.py app/tools/mutaciones.json --solo P3-1 P3-2
    python3 app/tools/mutar.py mis_mutaciones.json --salida resultados.json

El fichero es una lista de mutaciones:

    [{"id": "P3-1 F mergea el target sin sincronizar",
      "fichero": "app/phases/cmv40_pipeline.py",
      "viejo": "rpu_target_v40=rpu_target_effective,",
      "nuevo": "rpu_target_v40=rpu_target,",
      "modulos": ["test_cmv40_reglas_que_sobrevivian"]}]

`viejo` tiene que aparecer EXACTAMENTE una vez en el fichero; `modulos` son
módulos de `app/tests` por su nombre (como los descubre `discover`).

Lo que hace por su cuenta, porque cada punto costó un susto:

  · **Comprueba que los módulos están en verde ANTES de mutar.** Un «cazada»
    sobre un test que ya fallaba da confianza falsa justo donde se buscaba
    evidencia (pasó el 2026-09-28).
  · **Restaura desde el contenido original guardado**, no con `git
    checkout`: el checkout se lleva el trabajo sin commitear del fichero, y
    eso ha pasado seis veces. Guarda además una copia en disco ANTES de
    mutar, y la recupera al arrancar si una ejecución anterior murió a
    medias —parar una tanda a mano dejó una vez una mutación aplicada—.
  · **Cada ejecución lleva los directorios de la app aislados** (`CONFIG_DIR`,
    `TMP_DIR`, `OUTPUT_DIR`) y no escribe bytecode: un `.pyc` de la versión
    mutada, con el mismo tamaño y en el mismo segundo, se ejecutaría después
    de restaurar.
  · **Sale con 1 si alguna mutación sobrevive o no casa**, 2 si la base no
    está en verde, 0 si se cazaron todas. Un script que siempre sale con 0
    no se puede usar en un CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(RAIZ / "app" / "tests"))
from correr_suite import DIRECTORIOS as _DIRECTORIOS, FUERA_DEL_ENTORNO as _FUERA_DEL_ENTORNO  # noqa: E402


def _respaldo_dir(raiz: Path) -> Path:
    # `hashlib` y no `hash()`: el de un `str` cambia en cada proceso, y una
    # ejecución no encontraría nunca los respaldos de la anterior.
    clave = hashlib.sha1(str(raiz).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"mutar_respaldo_{clave}"


def recuperar_respaldos(raiz: Path) -> tuple[list[str], list[str]]:
    """Restaura los ficheros que una ejecución anterior dejó mutados.

    Devuelve `(recuperados, fallidos)`. **Un respaldo solo se borra si se
    pudo restaurar**: borraba los `.orig` siempre, así que un error de
    permisos al restaurar dejaba el fichero mutado y SIN su original
    (revisión de Codex de la suite, 2026-10-17)."""
    d = _respaldo_dir(raiz)
    recuperados, fallidos = [], []
    if not d.exists():
        return recuperados, fallidos
    for meta in sorted(d.glob("*.json")):
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fallidos.append(meta.name)
            continue
        copia = d / info.get("copia", "")
        try:
            (raiz / info["fichero"]).write_bytes(copia.read_bytes())
        except (OSError, KeyError):
            fallidos.append(info.get("fichero", meta.name))
            continue
        recuperados.append(info["fichero"])
        meta.unlink(missing_ok=True)
        copia.unlink(missing_ok=True)
    return recuperados, fallidos


def ejecutar(modulos: list[str], raiz: Path, python: str, timeout: float) -> tuple[int, str]:
    """Ejecuta los módulos con los directorios de la app en una caja propia."""
    caja = Path(tempfile.mkdtemp(prefix="mutar_caja_"))
    # Los MISMOS directorios y claves que aísla `correr_suite`: aquí solo se
    # fijaban tres de los siete, así que una mutación podía escribir en el
    # `LIBRARY_DIR` o el `ISOS_DIR` reales.
    env = dict(os.environ,
               PYTHONPATH=str(raiz / "app" / "tests"),
               PYTHONDONTWRITEBYTECODE="1",
               PYTHONPYCACHEPREFIX=str(caja / "pyc"))
    for nombre in _DIRECTORIOS:
        (caja / nombre.lower()).mkdir()
        env[nombre] = str(caja / nombre.lower())
    for nombre in _FUERA_DEL_ENTORNO:
        env.pop(nombre, None)
    r = subprocess.run([python, "-m", "unittest", *modulos], cwd=raiz / "app" / "tests",
                       capture_output=True, text=True, timeout=timeout, env=env)
    fallos = sorted({l.split(" ", 1)[1].split(" (")[0]
                     for l in r.stderr.splitlines() if l.startswith(("FAIL:", "ERROR:"))})
    resumen = next((l for l in reversed(r.stderr.splitlines())
                    if l.startswith(("OK", "FAILED"))), "")
    return r.returncode, (resumen + (" · " + ", ".join(fallos[:4]) if fallos else ""))


class _Interrumpido(Exception):
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mutaciones", type=Path)
    ap.add_argument("--solo", nargs="*", default=None,
                    help="ids (o prefijos de id) a ejecutar")
    ap.add_argument("--salida", type=Path, default=None,
                    help="JSON con el resultado de cada mutación")
    ap.add_argument("--raiz", type=Path, default=RAIZ, help=argparse.SUPPRESS)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args(argv)
    raiz = args.raiz.resolve()

    rec, fallidos = recuperar_respaldos(raiz)
    if rec:
        print(f"⚠ restaurados de una ejecución que murió a medias: {', '.join(rec)}")
    if fallidos:
        print(f"✗ no se pudieron restaurar (el respaldo se conserva en "
              f"{_respaldo_dir(raiz)}): {', '.join(fallidos)}")
        return 2

    muts = json.loads(args.mutaciones.read_text(encoding="utf-8"))
    if args.solo:
        muts = [m for m in muts if any(m["id"].startswith(p) for p in args.solo)]
    if not muts:
        print("no hay mutaciones que ejecutar")
        return 1

    # La base es CADA selección exacta de módulos, en su orden: la unión en
    # verde no garantiza que un subconjunto lo esté (un módulo que dependa de
    # lo que otro dejó al importarse falla solo), y una mutación asignada a
    # ese subconjunto se daría por «cazada» por un fallo previo (revisión de
    # Codex de la suite, 2026-10-17; medido entonces: 40 de 40 en verde).
    selecciones = list(dict.fromkeys(tuple(m["modulos"]) for m in muts))
    for sel in selecciones:
        rc, resumen = ejecutar(list(sel), raiz, args.python, args.timeout)
        if rc != 0:
            print(f"✗ la base no está en verde con {', '.join(sel)}: {resumen}")
            print("  una mutación «cazada» ahí no demostraría nada")
            return 2
    print(f"base: {len(selecciones)} selecciones de módulos en verde", flush=True)

    # SIGTERM se convierte en excepción para que el `finally` restaure.
    def _al_terminar(*_):
        raise _Interrumpido()
    signal.signal(signal.SIGTERM, _al_terminar)

    respaldo = _respaldo_dir(raiz)
    respaldo.mkdir(parents=True, exist_ok=True)
    resultados, vivas = [], 0
    for m in muts:
        f = raiz / m["fichero"]
        original = f.read_bytes()
        texto = original.decode("utf-8")
        n = texto.count(m["viejo"])
        if n != 1:
            print(f"?  NO CASA ({n} apariciones)  {m['id']}", flush=True)
            resultados.append({**m, "resultado": f"no casa ({n})"})
            vivas += 1
            continue
        copia = f"{int(time.time() * 1000)}.orig"
        (respaldo / copia).write_bytes(original)
        meta = respaldo / (copia + ".json")
        meta.write_text(json.dumps({"fichero": m["fichero"], "copia": copia}), encoding="utf-8")
        t0 = time.monotonic()
        try:
            f.write_text(texto.replace(m["viejo"], m["nuevo"]), encoding="utf-8")
            rc, detalle = ejecutar(m["modulos"], raiz, args.python, args.timeout)
        except (KeyboardInterrupt, _Interrumpido):
            f.write_bytes(original)
            meta.unlink(missing_ok=True)
            (respaldo / copia).unlink(missing_ok=True)
            print("✗ interrumpido: fichero restaurado")
            return 130
        finally:
            f.write_bytes(original)
        meta.unlink(missing_ok=True)
        (respaldo / copia).unlink(missing_ok=True)
        cazada = rc != 0
        vivas += 0 if cazada else 1
        print(f"{'✓ CAZADA   ' if cazada else '✗ SOBREVIVE'} {m['id']}  "
              f"({time.monotonic() - t0:.0f} s) {detalle}", flush=True)
        resultados.append({**m, "resultado": "cazada" if cazada else "sobrevive",
                           "detalle": detalle})
    if args.salida:
        args.salida.write_text(json.dumps(resultados, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    print(f"\n{len(muts) - vivas} de {len(muts)} cazadas")
    return 1 if vivas else 0


if __name__ == "__main__":
    sys.exit(main())
