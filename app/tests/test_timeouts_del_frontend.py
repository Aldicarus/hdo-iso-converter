"""Una llamada que hace trabajo pesado y se espera no puede llevar 30 s.

`apiFetch` tiene 30 s por defecto, que es lo correcto para navegación. Cuando
esos 30 s se le aplican a algo que monta un ISO, escanea los MPLS de un disco
o hace un `rmtree` de cientos de GB, lo que el usuario ve es **un timeout
mientras el servidor sigue trabajando perfectamente** — y el mensaje señala al
sitio equivocado: en el log del servidor no hay ni un error.

Caso real (2026-09-12), `disc-probe` sobre un BDMV de Juego de Tronos:

    17:14:21  arranca [interactivo] Detección de contenido del disco
    17:14:51  termina                                    (lleva 30 s)

Sin línea de acceso del POST, porque nunca completó: lo abortó el navegador y
FastAPI canceló el handler. Medido después con la ARC caliente son 21,5 s con
**3** candidatos, y el escaneo mira hasta 20.

Ya había pasado dos veces y se arregló **a mano y solo en el sitio que
dolía**: `/api/analyze` llevaba un `900000` suelto y `/api/mkv/analyze` un
`600000`… en UNA de sus dos llamadas. La otra —el re-análisis tras copiar
desde biblioteca— seguía en 30 s. De ahí este guard: la lista de lo que es
pesado ya existe y es `workload.CLASE_POR_RUTA`, así que no hace falta
mantener otra.

**Lo que decide no es cuánto dura, es quién espera la respuesta.** Un endpoint
fire-and-forget contesta al instante y su progreso viaja por otro canal; con
30 s le sobra. Por eso los exentos van en una lista explícita **con el motivo**,
no por patrón.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import workload  # noqa: E402
from frontend_sources import rutas  # noqa: E402

# Timeouts que cuentan como «generoso». `MKV_APPLY_LONG_TIMEOUT_MS` son 4 h y
# es suyo: una copia desde biblioteca son decenas de GB.
NOMBRES_LARGOS = ("API_FETCH_TIMEOUT_LARGO", "MKV_APPLY_LONG_TIMEOUT_MS")
MINIMO_MS = 60_000

# Rutas pesadas que el frontend llama y NO necesitan timeout largo, con el
# motivo. Todas comparten uno: **contestan al instante**.
RESPONDEN_AL_INSTANTE = {
    "POST /api/sessions/{session_id}/execute":
        "encola el rip y devuelve; el progreso va por el WS de la cola",
    "POST /api/mkv/quality-audit":
        "responde {queued}; el resultado viaja por el estado del job",
    "POST /api/create-series-sessions":
        "responde {queued}; la barra sale de /api/series-create-progress",
    "POST /api/cmv40/{session_id}/preflight-target":
        "responde {started}; el log va por el WS de la sesión",
    # `preflight-source` NO está: es pesado, pero el frontend no lo llama —
    # el pipeline usa la función directamente. Lo destapó el test de abajo.
    "POST /api/cmv40/{session_id}/analyze-source": "fase fire-and-forget (A)",
    "POST /api/cmv40/{session_id}/extract":        "fase fire-and-forget (C)",
    "POST /api/cmv40/{session_id}/apply-sync":     "fase fire-and-forget (E)",
    "POST /api/cmv40/{session_id}/inject":         "fase fire-and-forget (F)",
    "POST /api/cmv40/{session_id}/remux":          "fase fire-and-forget (G)",
    "POST /api/cmv40/{session_id}/validate":       "fase fire-and-forget (H)",
    "POST /api/cmv40/{session_id}/target-rpu-from-mkv":
        "fase fire-and-forget (B2)",
    "POST /api/cmv40/{session_id}/target-rpu-path":
        "mediana de 2 s sobre los proyectos del NAS (p90 10 s)",
    "POST /api/cmv40/{session_id}/target-rpu-from-drive":
        "mediana de 3 s sobre los proyectos del NAS (p90 10 s)",
}

# `apiFetch('<url>'|`<url>`, {…}[, timeout])`. Se captura hasta el `)` que
# cierra, contando llaves para no cortar en un `}` del cuerpo.
_APIFETCH = re.compile(r"apiFetch\(\s*([`'\"])([^`'\"]+)\1")


def _plantilla(url: str) -> str:
    """`/api/cmv40/${pid}/cleanup?x=1` → `/api/cmv40/{}/cleanup`."""
    url = url.split("?")[0]
    return re.sub(r"\$\{[^}]*\}", "{}", url).rstrip("/")


def _patron_de_ruta(ruta: str) -> str:
    """`/api/cmv40/{session_id}/cleanup` → `/api/cmv40/{}/cleanup`."""
    return re.sub(r"\{[^}]*\}", "{}", ruta).rstrip("/")


def _argumentos(src: str, desde: int) -> str:
    """El texto de la llamada desde `apiFetch(` hasta su `)`, equilibrado."""
    i = src.index("(", desde)
    prof = 0
    for j in range(i, min(len(src), i + 4000)):
        c = src[j]
        if c in "([{":
            prof += 1
        elif c in ")]}":
            prof -= 1
            if prof == 0:
                return src[i + 1:j]
    return ""


def _llamadas_pesadas():
    """(fichero, línea, ruta, argumentos) de cada apiFetch a una ruta pesada."""
    pesadas = {r: c for r, c in workload.CLASE_POR_RUTA.items()
               if c in (workload.CLASE_INTERACTIVO, workload.CLASE_DIFERIDO)}
    por_patron = {}
    for r in pesadas:
        metodo, camino = r.split(" ", 1)
        por_patron.setdefault(_patron_de_ruta(camino), []).append((r, metodo))

    for ruta in rutas():
        if not ruta.name.endswith(".js"):
            continue
        src = ruta.read_text(encoding="utf-8")
        for m in _APIFETCH.finditer(src):
            plant = _plantilla(m.group(2))
            if plant not in por_patron:
                continue
            args = _argumentos(src, m.start())
            candidatos = por_patron[plant]
            # Con varias rutas al mismo camino (GET/DELETE), se elige por el
            # `method:` que lleve la llamada; sin él, GET.
            met = re.search(r"method:\s*['\"](\w+)['\"]", args)
            met = met.group(1).upper() if met else "GET"
            elegida = next((r for r, mm in candidatos if mm == met), None)
            if elegida is None:
                continue
            n = src[:m.start()].count("\n") + 1
            yield ruta.name, n, elegida, args


def _tiene_timeout_largo(args: str) -> bool:
    """¿La llamada pasa un tercer argumento que sea un timeout generoso?"""
    if any(nombre in args for nombre in NOMBRES_LARGOS):
        return True
    # Un número suelto como tercer argumento.
    for num in re.findall(r",\s*(\d[\d_]{4,})\s*$", args.strip()):
        if int(num.replace("_", "")) >= MINIMO_MS:
            return True
    return False


class TestNingunaLlamadaPesadaSeQuedaEnTreintaSegundos(unittest.TestCase):

    def test_o_lleva_timeout_largo_o_esta_justificada(self):
        malas = []
        for fichero, linea, ruta, args in _llamadas_pesadas():
            if ruta in RESPONDEN_AL_INSTANTE:
                continue
            if _tiene_timeout_largo(args):
                continue
            malas.append(f"{fichero}:{linea}  {ruta}")
        self.assertEqual(sorted(malas), [], "\n  ".join(
            ["", "con 30 s: el usuario verá un timeout mientras el servidor "
                 "sigue trabajando —", "añade API_FETCH_TIMEOUT_LARGO, o "
                 "justifícalo en RESPONDEN_AL_INSTANTE:"] + sorted(malas)))

    def test_el_guard_esta_mirando_algo(self):
        """Si el cruce dejara de encontrar llamadas, pasaría en verde vacío."""
        encontradas = list(_llamadas_pesadas())
        self.assertGreater(len(encontradas), 15,
                           f"solo {len(encontradas)} llamadas cruzadas: "
                           "¿ha cambiado la forma de llamar a apiFetch?")

    def test_disc_probe_es_uno_de_los_vigilados(self):
        """El que provocó todo esto. Sin él, el guard no cubre el caso real."""
        rutas_vistas = {r for _f, _l, r, _a in _llamadas_pesadas()}
        self.assertIn("POST /api/disc-probe", rutas_vistas)


class TestLaListaDeExentosNoSePudre(unittest.TestCase):
    """Una excepción que ya no corresponde a nada parece cobertura y no la es.

    Es la misma regla que la lista (vacía) de `test_event_loop_limpio`.
    """

    def test_todo_exento_es_una_ruta_pesada_de_verdad(self):
        pesadas = {r for r, c in workload.CLASE_POR_RUTA.items()
                   if c in (workload.CLASE_INTERACTIVO, workload.CLASE_DIFERIDO)}
        huerfanas = sorted(set(RESPONDEN_AL_INSTANTE) - pesadas)
        self.assertEqual(huerfanas, [],
                         f"exentas que ya no son rutas pesadas: {huerfanas}")

    def test_todo_exento_lo_llama_el_frontend(self):
        llamadas = {r for _f, _l, r, _a in _llamadas_pesadas()}
        # `mkv/apply` no entra: su timeout lo elige la rama (copia sí, edición
        # no), así que se comprueba en el test de arriba y no aquí.
        sin_uso = sorted(set(RESPONDEN_AL_INSTANTE) - llamadas)
        self.assertEqual(sin_uso, [],
                         f"exentas que el frontend ya no llama: {sin_uso}")

    def test_cada_exento_dice_por_que(self):
        for ruta, motivo in RESPONDEN_AL_INSTANTE.items():
            self.assertGreater(len(motivo), 20, f"{ruta}: motivo demasiado corto")


if __name__ == "__main__":
    unittest.main()
