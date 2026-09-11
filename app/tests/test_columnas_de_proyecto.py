"""Las tres columnas de proyecto, con el lenguaje de la columna de trabajo.

Las tres pintaban el MISMO HTML copiado tres veces, y en las tres se iban
**dos filas enteras en fechas rotuladas** («Modif.», «Ejecuc.», «Analiz.»)
mientras lo que distingue un proyecto de otro no aparecía en ninguna: Tab 1 no
decía qué episodio es, Tab 3 no decía si el upgrade es un drop-in de treinta
segundos o un merge de hora y media, y ninguna tenía carátula aunque la ficha
de TMDb esté en el listado (35 de 44 sesiones y 116 de 117 proyectos la
traen).

Hoy hay una sola tarjeta —`tarjetaDeProyecto`, en `core.js`— y lo que este
fichero fija es lo que se decidió al unificarla:

- **Los tags del nombre salen del título y pasan a etiquetas.** Van al final
  (`Peli (2026) [DV FEL] [Audio DCP].mkv`) y el título se recorta por la
  derecha, así que eran lo primero que se perdía. Y son lo que distingue una
  versión de otra.
- **El número de episodio es una etiqueta, no parte del título.** Pegado al
  título, «Juego de tronos (2011) · S02E10» se corta justo en el episodio, que
  es lo único que separa esa fila de las otras nueve del mismo disco.
- **El acento lateral lleva el ESTADO, no la pestaña** —al revés que
  `.wb-card`—, porque dentro de un sidebar todas las filas son de la misma
  pestaña y ese color sería constante. Y se mueve SIEMPRE junto al chip: si
  uno dice una cosa y el otro otra, la fila se lee mal de un vistazo.
- **Los puntitos de fase son para lo que está a medias.** En un proyecto
  terminado están todos llenos y no contestan nada.
- **La carátula se pide al ancho que se ve.** La ficha guarda la de 342 px y
  en la columna ocupa 36: sin reescribir el ancho son 117 imágenes de un
  tamaño que no se ve.

Se mide en Chrome sobre el `index.html` real porque es la única forma de que
un fallo de carga o un `innerHTML` mal montado se vea; leer el fuente no lo
demostraría. Lo de Tab 2 está en `test_tab2_columna_izquierda.py`.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_columnas_de_proyecto -v
"""
import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)


# ── Los datos: la variedad que hay en el NAS ────────────────────────────────
#
# Una película con tags, un episodio de una serie, uno sin ejecutar y uno con
# error. Los nombres y los campos son los de las sesiones reales.
SESIONES = [
    {"id": "s_peli", "mkv_name": "Minions & Monsters (2026) [DV FEL] [Audio DCP].mkv",
     "status": "done", "media_type": "movie", "execution_history": [{"status": "done"}],
     "updated_at": "2026-09-10T21:45:03Z", "last_executed": "2026-09-10T21:45:02Z",
     "tmdb_info": {"poster_url": "https://image.tmdb.org/t/p/w342/abc.jpg"}},
    {"id": "s_serie", "mkv_name": "Juego de tronos (2011) - S02e10 - Valar Morghulis [DV FEL].mkv",
     "status": "done", "media_type": "series", "series_name": "Juego de tronos",
     "series_year": 2011, "season_number": 2, "episode_number": 10,
     "episode_title": "Valar Morghulis", "execution_history": [{"status": "done"}],
     "updated_at": "2026-06-08T10:00:00Z", "last_executed": "2026-06-08T10:00:00Z",
     "tmdb_info": {"poster_url": "https://image.tmdb.org/t/p/w342/got.jpg"}},
    {"id": "s_pend", "mkv_name": "El día de la revelación (2026) [DV FEL].mkv",
     "status": "pending", "media_type": "movie", "execution_history": [],
     "updated_at": "2026-09-04T09:00:00Z", "tmdb_info": {}},
    {"id": "s_err", "mkv_name": "Un fallo (2020).mkv", "status": "pending",
     "media_type": "movie", "execution_history": [{"status": "error"}],
     "updated_at": "2026-09-01T09:00:00Z"},
]

CMV40 = [
    {"id": "c_medio", "source_mkv_name": "Imaginary (2024) [Audio DCP].mkv",
     "phase": "target_provided", "running_phase": None, "archived": False,
     "error_message": "", "output_workflow": "restore_merge",
     "target_l8_quality_tier": "full", "updated_at": "2026-08-15T10:00:00Z",
     "tmdb_info": {"poster_url": "https://image.tmdb.org/t/p/w342/im.jpg"}},
    {"id": "c_corriendo", "source_mkv_name": "Predator (2026).mkv",
     "phase": "extracted", "running_phase": "inject", "archived": False,
     "error_message": "", "output_workflow": "restore_dropin",
     "updated_at": "2026-09-11T08:00:00Z", "tmdb_info": {}},
    {"id": "c_archivado", "source_mkv_name": "Supergirl (2026) [Audio DCP].mkv",
     "phase": "done", "running_phase": None, "archived": True,
     "error_message": "", "output_workflow": "keep_cmv29",
     "target_l8_quality_tier": "core", "updated_at": "2026-09-11T08:25:44Z",
     "tmdb_info": {"poster_url": "https://image.tmdb.org/t/p/w342/sg.jpg"}},
    {"id": "c_error", "source_mkv_name": "Roto (2019).mkv", "phase": "extracted",
     "running_phase": None, "archived": False, "error_message": "algo falló",
     "output_workflow": "", "updated_at": "2026-09-01T08:00:00Z"},
]

_SONDA = ("<script>window.__errores=[];"
          "window.addEventListener('error',e=>window.__errores.push("
          "(e.message||'')+' @ '+(e.filename||'').split('/').pop()+':'+e.lineno));"
          "window.fetch=()=>new Promise(()=>{});"
          "window.WebSocket=function(){this.close=()=>{};};</script>")

_CUERPO = """
<pre id="__out"></pre>
<script>
(function () {
  const leer = sel => [...document.querySelectorAll(sel)].map(c => ({
    titulo: (c.querySelector('.session-card-title') || {}).textContent || '',
    sub: (c.querySelector('.proj-sub') || {}).textContent || '',
    chips: [...c.querySelectorAll('.proj-chip')].map(e => e.textContent.trim()),
    // El tono del chip de ESTADO, que es el que cambia de fila a fila (el de
    // la miniatura lleva el color de la pestaña y es siempre el mismo).
    // `icono-chip icono-rojo icono-chip-sm`: el tono es el que NO es
    // «chip» ni el sufijo de tamaño.
    tono: ((c.querySelector('.proj-estado .icono-chip') || {}).className || '')
            .match(/icono-(?!chip)(\\w+)/)?.[1] || '',
    acento: [...c.classList].find(x => x.startsWith('estado-')) || '',
    pips: c.querySelectorAll('.proj-pip').length,
    img: (c.querySelector('.proj-mini img') || {}).getAttribute
           ? c.querySelector('.proj-mini img').getAttribute('src') : '',
    lazy: (c.querySelector('.proj-mini img') || {}).getAttribute
           ? c.querySelector('.proj-mini img').getAttribute('loading') : '',
    icono: !!c.querySelector('.proj-mini .icono-chip'),
    fecha: (c.querySelector('.proj-fecha') || {}).textContent || '',
  }));
  setTimeout(() => {
    const out = {errores: [], tab1: [], tab3: [], puro: {}};
    try {
      _sessionsCache = window.__SES; renderSidebarSessions(window.__SES);
      _cmv40SidebarList = window.__CM; _renderCMv40Sidebar();
      out.tab1 = leer('#sessions-list .session-card');
      out.tab3 = leer('#cmv40-sidebar-list .session-card');
      out.puro = {
        tags: nombreYTags('Peli (2026) [DV FEL] [Audio DCP].mkv'),
        sinTags: nombreYTags('Simple.mkv'),
        raro: nombreYTags('[Solo tags].mkv'),
        mini: miniaturaDe('https://image.tmdb.org/t/p/w342/x.jpg'),
        miniOtra: miniaturaDe('https://ejemplo/x.jpg'),
        miniVacia: miniaturaDe(''),
      };
    } catch (e) { out.errores.push('EXCEPCIÓN: ' + e.message); }
    out.errores = out.errores.concat(window.__errores);
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
})();
</script>
"""


def _medir() -> dict:
    pagina = html().replace("</head>", _SONDA + "</head>")
    datos = (f"<script>window.__SES={json.dumps(SESIONES)};"
             f"window.__CM={json.dumps(CMV40)};</script>")
    pagina = pagina.replace("</body>", datos + _CUERPO + "</body>")
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                      encoding="utf-8",
                                      dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu",
             "--allow-file-access-from-files", "--dump-dom",
             "--window-size=1280,1000", "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(_html.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class ColumnasCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def t1(self, sid):
        """La tarjeta de Tab 1 por el orden en que se le pasan las sesiones."""
        return self.m["tab1"][[s["id"] for s in SESIONES].index(sid)]

    def t3(self, sid):
        return next(c for c in self.m["tab3"]
                    if c["titulo"].startswith(
                        {"c_medio": "Imaginary", "c_corriendo": "Predator",
                         "c_archivado": "Supergirl", "c_error": "Roto"}[sid]))


class TestNingunaColumnaPetaAlPintarse(ColumnasCase):

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])

    def test_y_se_pinta_una_tarjeta_por_proyecto(self):
        self.assertEqual(len(self.m["tab1"]), len(SESIONES))
        self.assertEqual(len(self.m["tab3"]), len(CMV40))


class TestLosHelpersDelNombre(ColumnasCase):

    def test_los_tags_se_separan_del_titulo(self):
        p = self.m["puro"]["tags"]
        self.assertEqual(p["titulo"], "Peli (2026)")
        self.assertEqual(p["tags"], ["DV FEL", "Audio DCP"])

    def test_un_nombre_sin_tags_se_queda_igual(self):
        self.assertEqual(self.m["puro"]["sinTags"],
                         {"titulo": "Simple", "tags": []})

    def test_un_nombre_que_es_SOLO_tags_no_se_queda_sin_titulo(self):
        """Quitar los tags no puede dejar la tarjeta sin nada que leer."""
        self.assertEqual(self.m["puro"]["raro"]["titulo"], "[Solo tags]")

    def test_la_caratula_se_pide_al_ancho_que_se_ve(self):
        self.assertEqual(self.m["puro"]["mini"],
                         "https://image.tmdb.org/t/p/w92/x.jpg")

    def test_una_url_que_no_es_de_tmdb_no_se_toca(self):
        self.assertEqual(self.m["puro"]["miniOtra"], "https://ejemplo/x.jpg")
        self.assertEqual(self.m["puro"]["miniVacia"], "")


class TestLaColumnaDeTab1(ColumnasCase):

    def test_los_tags_salen_del_titulo_y_pasan_a_etiquetas(self):
        c = self.t1("s_peli")
        self.assertEqual(c["titulo"], "Minions & Monsters (2026)")
        self.assertIn("DV FEL", c["chips"])
        self.assertIn("Audio DCP", c["chips"])

    def test_de_una_serie_el_titulo_es_la_serie_y_el_episodio_una_etiqueta(self):
        """Pegado al título se corta justo ahí, y es lo único que separa esa
        fila de las otras nueve del mismo disco."""
        c = self.t1("s_serie")
        self.assertEqual(c["titulo"], "Juego de tronos (2011)")
        self.assertEqual(c["chips"][0], "S02E10")

    def test_y_el_subtitulo_de_una_serie_es_el_titulo_del_episodio(self):
        self.assertEqual(self.t1("s_serie")["sub"], "Valar Morghulis")

    def test_de_una_pelicula_el_subtitulo_dice_el_estado(self):
        """El chip solo lo dice al pasar el ratón; en texto se lee de golpe."""
        self.assertEqual(self.t1("s_peli")["sub"], "Completado")
        self.assertEqual(self.t1("s_pend")["sub"], "Sin ejecutar")

    def test_el_chip_y_el_acento_dicen_lo_MISMO(self):
        hecho, pend, err = self.t1("s_peli"), self.t1("s_pend"), self.t1("s_err")
        self.assertEqual((hecho["tono"], hecho["acento"]), ("verde", "estado-hecho"))
        self.assertEqual((pend["tono"], pend["acento"]), ("gris", ""))
        self.assertEqual((err["tono"], err["acento"]), ("rojo", "estado-error"))

    def test_la_caratula_va_al_ancho_de_la_miniatura_y_diferida(self):
        c = self.t1("s_peli")
        self.assertEqual(c["img"], "https://image.tmdb.org/t/p/w92/abc.jpg")
        self.assertEqual(c["lazy"], "lazy")

    def test_sin_caratula_queda_el_icono_y_no_un_hueco(self):
        c = self.t1("s_err")
        self.assertEqual(c["img"], "")
        self.assertTrue(c["icono"])

    def test_la_fecha_esta_y_no_lleva_rotulo(self):
        """«Modif.» delante de una fecha relativa no añade nada; la completa
        sigue en el tooltip."""
        self.assertTrue(self.t1("s_peli")["fecha"])
        self.assertNotIn("Modif", self.t1("s_peli")["fecha"])


class TestLaColumnaDeTab3(ColumnasCase):

    def test_el_subtitulo_es_la_fase_aunque_este_archivado(self):
        """«Archivado» ya lo dice su chip, y repetirlo costaba el único dato
        que la fila tenía: en qué punto se quedó."""
        self.assertEqual(self.t3("c_archivado")["sub"], "Completado")
        self.assertEqual(self.t3("c_archivado")["tono"], "gris")

    def test_lo_que_corre_dice_QUE_corre_y_no_la_ultima_fase_hecha(self):
        c = self.t3("c_corriendo")
        self.assertEqual(c["tono"], "verde")
        self.assertEqual(c["acento"], "estado-curso")
        self.assertNotEqual(c["sub"], "BL/EL extraídos")

    def test_la_clase_de_upgrade_va_en_una_etiqueta(self):
        """Separa un proyecto de treinta segundos de uno de hora y media, y
        antes había que abrirlo para saberlo."""
        self.assertIn("Merge", self.t3("c_medio")["chips"])
        self.assertIn("Drop-in", self.t3("c_corriendo")["chips"])
        self.assertIn("Se mantiene", self.t3("c_archivado")["chips"])

    def test_y_el_tier_del_L8_tambien(self):
        self.assertIn("CMv4 FULL", self.t3("c_medio")["chips"])
        self.assertIn("CMv4 CORE", self.t3("c_archivado")["chips"])

    def test_los_puntitos_solo_en_lo_que_esta_a_medias(self):
        self.assertGreater(self.t3("c_medio")["pips"], 0)
        self.assertEqual(self.t3("c_archivado")["pips"], 0,
                         "en un proyecto terminado están todos llenos y no "
                         "contestan nada")

    def test_un_error_sin_resolver_manda_sobre_la_fase(self):
        c = self.t3("c_error")
        self.assertEqual((c["tono"], c["acento"]), ("rojo", "estado-error"))


if __name__ == "__main__":
    unittest.main()
