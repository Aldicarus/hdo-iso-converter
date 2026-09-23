"""La card «🛡️ Validaciones» del Tab 3, renderizada de verdad en node.

Antes esta card enseñaba seis veredictos y ningún dato. El caso que lo
destapó fue The Mandalorian and Grogu (2026-09-04): el gate L5 decidió
comparando **un** frame y la card lo resumía como «1/1 muestras coinciden ·
el patrón de active area es idéntico en ambos masters» — una afirmación que
un frame no puede sostener, y sin forma de auditarla desde la pantalla.

Aquí se fija que la card es un volcado: los cinco bloques, los dos RPU lado a
lado aunque todo pase, los umbrales junto a los valores y —lo que la hace
accionable— los **timecodes** de los tramos divergentes, para que el usuario
pueda abrir el MKV en ese punto y mirarlo.

Se evalúan las funciones REALES de `tab3.js` en node, con las fuentes que
`frontend_sources` saca de `index.html` (no una ruta hardcodeada, que se
desincronizaría).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_card_validaciones -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import argv_node, js_completo, motor_i18n, pintar_textos_es, sistema_de_iconos  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()

# Todo lo que la card necesita, en orden de dependencia. Se extraen del
# fuente real: si alguna se renombra, el test falla en vez de medir otra cosa.
FUNCIONES = (
    "escHtml",
    "_cmv40Plan", "_cmv40Trust", "_cmv40DropIn",
    "_cmv40GateRowHtml",
    "_cmv40Timecode", "_cmv40Num", "_cmv40Dur", "_cmv40L5Tupla",
    "_cmv40CmpMarca", "_cmv40RpuFila", "_cmv40BloqueHead", "_cmv40SkipLabel",
    "_cmv40GateBloque1", "_cmv40GateBloque2", "_cmv40GateFilaHtml",
    "_cmv40GateBloque3", "_cmv40GateBloque4", "_cmv40GateBloque5",
    "_cmv40GateDiagnosticoTexto",
    "_cmv40RenderGateCardBC",
)


def _extraer(nombre: str) -> str:
    marca = f"function {nombre}("
    i = JS.index(marca)
    return JS[i:JS.index("\n}\n", i) + 3]


def _constante(nombre: str) -> str:
    m = re.search(rf"^const {nombre} = \[.*?\];", JS, re.S | re.M)
    if not m:
        raise AssertionError(f"no encuentro la constante {nombre}")
    return m.group(0) + "\n"


# ── la sesión de referencia: los números REALES de Mandalorian ─────────
#
# Medidos sobre los dos RPU del NAS el 2026-09-04. Si un test de aquí falla,
# hay un disco de verdad que se pintaría distinto.
def sesion_mandalorian(**over):
    s = {
        "id": "cmv40_The_Mandalorian_and_Grogu_2026_1788511616",
        "phase": "done",
        "output_mkv_name": "The Mandalorian and Grogu (2026) [CMv4 FULL].mkv",
        "source_workflow": "p7_fel",
        "target_type": "trusted_p7_fel_final",
        "target_trust_ok": True,
        "trust_override": "auto",
        "output_workflow": "restore_dropin",
        "source_fps": 23.976,
        "phases_skipped": ["demux_dual_layer", "mux_dual_layer",
                           "per_frame_data_skipped", "sync_verification_pause",
                           "merge_cmv40_transfer"],
        "source_dv_info": {
            "profile": 7, "el_type": "FEL", "cm_version": "v2.9",
            "frame_count": 190021, "scene_count": 2313,
            "has_l1": True, "has_l2": True, "has_l5": True, "has_l6": True,
            "has_l8": False, "has_l9": True,
            "l1_max_cll": 450.89, "l1_max_fall": 215.23,
            "l5_top": 275, "l5_bottom": 275, "l6_max_cll": 0,
        },
        "target_dv_info": {
            "profile": 7, "el_type": "FEL", "cm_version": "v4.0",
            "frame_count": 190021, "scene_count": 2313,
            "has_l1": True, "has_l2": True, "has_l5": True, "has_l6": True,
            "has_l8": True, "has_l9": True,
            "l1_max_cll": 450.89, "l1_max_fall": 215.23,
            "l5_top": 0, "l5_bottom": 0, "l6_max_cll": 0,
            "l8_trim_nits": [100], "l8_target_indices": [1],
            "l9_primaries": "DCI-P3 D65",
        },
        "target_trust_gates": {
            "frames": {"ok": True, "bd": 190021, "target": 190021,
                       "critical": True, "severity": "ok", "why": ""},
            "cm_version": {"ok": True, "value": "v4.0", "critical": True,
                           "severity": "ok", "why": ""},
            "has_l8": {"ok": True, "critical": True, "severity": "ok", "why": ""},
            "l5_div": {
                "ok": True, "px_max": 275, "soft_px": 5, "critical_px": 30,
                "critical": True, "severity": "warn",
                "why": "Comparados 190.021 frames: 593 divergen (0,31%).",
                "sampled_method": "per_frame_completo",
                "perfil_source": {"frames_con_bloque": 113247, "sin_bloque": 76774,
                                  "valores": [[[275, 275, 0, 0], 113247]],
                                  "variable": True},
                "perfil_target": {"frames_con_bloque": 190021, "sin_bloque": 0,
                                  "valores": [[[275, 275, 0, 0], 113840],
                                              [[0, 0, 0, 0], 76181]],
                                  "variable": True},
                "comparados": 190021, "divergentes": 593,
                "por_zona": {"intro": [24, 9502], "body": [558, 171018],
                             "outro": [11, 9501]},
                "body_coverage": 0.9967,
                "tramos": [[14660, 14937, 278, "body"],
                           [102962, 103115, 154, "body"],
                           [176679, 176804, 126, "body"],
                           [0, 23, 24, "intro"],
                           [190010, 190020, 11, "outro"]],
                "mayor_tramo": {"frames": 278, "segundos": 11.6,
                                "zona": "body", "desde": 14660},
                "umbral_tramo_segundos": 40, "fps": 23.976,
                "procedencia": {"declara_l5_variable": True,
                                "tokens": ["variable l5"], "contradice": False,
                                "nota_sheet_l5": "L5 edit: https://justpaste.it/hecft"},
            },
            "l6_div": {"ok": True, "nits_diff": 0, "threshold": 50,
                       "critical": False, "severity": "ok", "why": ""},
            "l1_div": {"ok": True, "pct_diff": 0.0, "threshold_pct": 5.0,
                       "critical": False, "severity": "ok", "why": ""},
        },
        "l2_comparison": "identical",
        "source_l2_unique_count": 3545, "target_l2_unique_count": 3545,
        "target_l2_target_pqs": [2081, 2851, 3079],
        "target_l8_classification": "real", "target_l8_quality_label": "CMv4 FULL",
        "target_l8_unique_count": 1018, "target_l8_neutral_frames_pct": 0.1429,
        "target_l8_has_mid_contrast": True, "target_l8_has_clip_trim": True,
        "target_rpu_source_label": "drive://abc/Mandalorian VARIABLE L5.bin",
        "source_preflight_ok": True, "target_preflight_ok": True,
        "preflight_decision": "ok",
        "recommended_action_label": "Inyectar RPU CMv4.0 (rápido)",
        "sheet_recommendation": {"status": "recommended", "rows": [
            {"dv_source": "itunes", "sync_offset": "0.0",
             "notes": "L5 edit: https://justpaste.it/hecft"}]},
    }
    s.update(over)
    return s


@unittest.skipUnless(NODE, "node no disponible")
def _fila_de(html, etiqueta):
    """El trozo de HTML de UNA fila del bloque ②.

    Afirmar sobre la card entera no sirve para la columna de la derecha: un
    `≠` de otra fila da el test por bueno (o por malo) sin haber mirado la
    que interesa.
    """
    i = html.find(">" + etiqueta + "<")
    assert i != -1, f"no está la fila «{etiqueta}»"
    fin = html.find("</div>", html.find("text-align:right", i))
    return html[i:fin]


class CardTestCase(unittest.TestCase):

    def render(self, session, expandida=True):
        script = (motor_i18n() + sistema_de_iconos()
                  + _constante("CMV40_PHASES_ORDER")
                  + "".join(_extraer(f) for f in FUNCIONES)
                  + "\nprocess.stdout.write(_cmv40RenderGateCardBC("
                    "'p1', JSON.parse(process.argv[2]), "
                  + ("true" if expandida else "false") + "));")
        r = subprocess.run(argv_node(script, json.dumps(session)),
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[:600]}")
        return pintar_textos_es(r.stdout)

    def diagnostico(self, session):
        script = (motor_i18n() + sistema_de_iconos()
                  + _constante("CMV40_PHASES_ORDER")
                  + "".join(_extraer(f) for f in FUNCIONES)
                  + "\nprocess.stdout.write(_cmv40GateDiagnosticoTexto("
                    "JSON.parse(process.argv[2])));")
        r = subprocess.run(argv_node(script, json.dumps(session)),
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[:600]}")
        return pintar_textos_es(r.stdout)


class TestLosCincoBloques(CardTestCase):

    def test_aparecen_los_cinco(self):
        html = self.render(sesion_mandalorian())
        for num, titulo in (("①", "Veredicto y consecuencia"),
                            ("②", "Los dos RPU, lado a lado"),
                            ("③", "Gates"),
                            ("④", "Desglose L5"),
                            ("⑤", "Evidencia de apoyo")):
            self.assertIn(num, html, f"falta el bloque {num}")
            self.assertIn(titulo, html, f"falta el título «{titulo}»")

    def test_el_bloque_2_se_pinta_aunque_todo_pase(self):
        """La queja original: en un job correcto no se veía ni un dato."""
        s = sesion_mandalorian()
        for g in s["target_trust_gates"].values():
            g["ok"], g["severity"] = True, "ok"
        html = self.render(s)
        self.assertIn("Los dos RPU, lado a lado", html)
        self.assertIn("BD (source)", html)
        self.assertIn("Bin (target)", html)
        # y con datos de verdad dentro
        self.assertIn("190.021", html)
        self.assertIn("2.313", html)
        self.assertIn("DCI-P3 D65", html)

    def test_el_bloque_1_dice_QUE_fases_se_omiten(self):
        html = self.render(sesion_mandalorian())
        self.assertIn("revisión visual de sync (Fases D/E)", html)
        self.assertIn("merge CMv4.0 (Fase F)", html)
        self.assertIn("restore_dropin", html)

    def test_los_gates_muestran_umbral_y_severidad(self):
        html = self.render(sesion_mandalorian())
        self.assertIn("umbral", html)
        self.assertIn("≤ 50 nits", html)
        self.assertIn("≤ 5%", html)
        self.assertIn("crítico", html)


class TestElL3SaleEnLaTabla(CardTestCase):
    """El L3 no aparecía por ninguna de las dos columnas, y el bin lo tiene.

    Reportado el 2026-09-20 sobre la ficha de un proyecto: la tabla listaba
    L1, L5, L6, L8, L9 y L11, y la fila «Niveles» decía «L1 L2 L5 L6» contra
    «L1 L2 L5 L6 L8 L9». Dos causas encadenadas:

    · **`dovi_tool info --summary` no emite L3** —emite exactamente cuatro
      líneas de niveles: `L5 offsets`, `L2 trims`, `L8 trims`, `L9 MDP`— así
      que el parser del summary no puede poblar `has_l3`, y es de ahí de
      donde salen los dos `*_dv_info` de esta pestaña. El arreglo del
      2026-09-18 llegó al análisis extendido de Tab 2 y **no** aquí.
    · y la tabla **no tenía fila de L3**, así que aunque el flag llegara no
      había dónde verlo.

    Medido sobre el NAS: Pulp Fiction tiene **485 combos L3** en el bin y
    llegaba con `has_l3=False`.
    """

    def _con_l3(self, src=None, tgt=None):
        s = sesion_mandalorian()
        for lado, v in (("source_dv_info", src), ("target_dv_info", tgt)):
            if v is not None:
                s[lado] = {**s[lado], **v}
        return self.render(s)

    def test_la_fila_existe(self):
        self.assertIn("L3 tonos medios", self._con_l3())

    def test_el_bin_medido_enseña_su_cuenta(self):
        html = self._con_l3(tgt={"l3_medido": True, "l3_unique_count": 485,
                                 "l3_frames": 190021})
        self.assertIn("485 ajustes", html)

    def test_sin_medir_no_se_lee_como_ausente(self):
        """Es la distinción que faltaba: un guion decía «no lo tiene» de algo
        que nadie había mirado."""
        html = self._con_l3()
        self.assertIn("sin medir", html)
        self.assertNotIn("sin L3", html)

    def test_medido_y_vacio_lo_dice(self):
        html = self._con_l3(src={"l3_medido": True, "l3_unique_count": 0})
        self.assertIn("sin L3", html)

    def test_lo_que_el_bin_APORTA_se_marca(self):
        """Con el disco sin L3 y el bin con L3, la fila lo dice: es justo lo
        que este bloque contesta."""
        html = self._con_l3(src={"l3_medido": True, "l3_unique_count": 0},
                            tgt={"l3_medido": True, "l3_unique_count": 485})
        self.assertIn("↑ upgrade", html)

    def test_sin_medir_el_disco_TAMBIEN_se_marca(self):
        """Invierte lo que este test pedía hasta el 2026-09-23.

        La regla era «sin saber qué trae el disco, decir que el bin aporta
        L3 sería una conclusión sacada de un hueco», y suena bien. Pero la
        alternativa NO era no decir nada: sin marca propia la fila cae al
        comparador genérico, que compara «sin medir» con «485 ajustes», los
        ve distintos y pinta **`≠` en ámbar**. O sea que «no lo hemos
        mirado» acababa en pantalla como «discrepan» — peor que la
        conclusión que se quería evitar. Lo vio el usuario mirando la card.

        Y la marca no sale de un hueco: sobre un RPU P7 MEL CM v2.9 real el
        export de `level3` sale VACÍO, así que el Blu-ray no trae L3 y el
        bin lo aporta. Está medido y escrito en CLAUDE.md.
        """
        fila = _fila_de(self._con_l3(tgt={"l3_medido": True,
                                          "l3_unique_count": 485}),
                        "L3 tonos medios")
        self.assertIn("↑ upgrade", fila)
        # Sobre la card entera esto no probaría nada: el L5 de esta fixture
        # diverge de verdad y lleva su propio `≠`.
        self.assertNotIn("≠", fila)

    def test_si_nadie_lo_midio_no_se_afirma_que_coinciden(self):
        """Los dos a «sin medir» son dos huecos, no un acuerdo: un ✓ verde
        diría que se ha comprobado que son iguales."""
        html = self._con_l3()
        fila = _fila_de(html, "L3 tonos medios")
        self.assertNotIn("check", fila, f"marca indebida en: {fila}")
        self.assertNotIn("≠", fila)

    def test_y_la_fila_de_niveles_lo_incluye(self):
        html = self._con_l3(tgt={"l3_medido": True, "l3_unique_count": 485,
                                 "has_l3": True})
        # `niveles()` compone «L1 L2 L3 …» filtrando por `has_lN`.
        self.assertRegex(html, r"L1 L2 L3\b")


class TestTimecodes(CardTestCase):
    """Lo que convierte «558 frames divergen» en algo que se puede ir a mirar."""

    def test_el_timecode_del_mayor_tramo(self):
        # 14660 / 23.976 = 611,4 s = 10 min 11 s
        html = self.render(sesion_mandalorian())
        self.assertIn("00:10:11", html)

    def test_todos_los_tramos_llevan_timecode(self):
        # Los cinco tramos reales de Mandalorian a 23,976 fps. El primero es
        # comprobable a mano (14660/23,976 = 611,4 s = 10 min 11 s) y sirve de
        # ancla; los otros salen de la misma división.
        html = self.render(sesion_mandalorian())
        for tc in ("00:10:11", "01:11:34", "02:02:48", "00:00:00", "02:12:05"):
            self.assertIn(tc, html, f"falta el timecode {tc}")

    def test_sin_fps_no_revienta(self):
        s = sesion_mandalorian()
        s["target_trust_gates"]["l5_div"].pop("fps")
        s.pop("source_fps")
        self.assertIn("00:10:11", self.render(s))   # cae al 23.976 por defecto


class TestDesgloseL5(CardTestCase):

    def test_muestra_los_perfiles_y_la_cobertura(self):
        html = self.render(sesion_mandalorian())
        self.assertIn("Perfil BD (source)", html)
        self.assertIn("Perfil bin (target)", html)
        self.assertIn("sin bloque → neutro", html)      # la semántica que faltaba
        self.assertIn("99.67%", html)
        self.assertIn("umbral 40s", html)

    def test_sesion_vieja_con_el_muestreo_de_24_no_revienta(self):
        s = sesion_mandalorian()
        s["target_trust_gates"]["l5_div"] = {
            "ok": True, "px_max": 275, "soft_px": 5, "critical": True,
            "severity": "warn", "why": "Muestreo de 1 frames: 1 coinciden.",
            "sampled_method": "per_frame_zoned_24",
            "sampled_total": 1, "sampled_matches": 1,
            "sampled_zone_counts": {"intro": 1, "body": 0, "outro": 0},
            "sampled_zone_mismatches": {"intro": 0, "body": 0, "outro": 0},
            "sampled_body_coverage": 1.0,
        }
        html = self.render(s)
        self.assertIn("muestreo antiguo", html)
        self.assertIn("1 coinciden de 1", html)
        self.assertNotIn("Mayor tramo contiguo", html)

    def test_sin_refinamiento_no_pinta_el_bloque_4(self):
        s = sesion_mandalorian()
        s["target_trust_gates"]["l5_div"] = {
            "ok": True, "px_max": 0, "soft_px": 5, "critical": True,
            "severity": "ok", "why": "",
        }
        html = self.render(s)
        self.assertNotIn("Desglose L5", html)
        self.assertIn("Los dos RPU, lado a lado", html)   # el resto sigue


class TestAvisoDeProcedencia(CardTestCase):
    """Precisión 100% sobre 99 proyectos: si el nombre lo declara y nosotros
    no lo medimos, el error es nuestro."""

    def test_contradice_pinta_el_aviso(self):
        s = sesion_mandalorian()
        s["target_trust_gates"]["l5_div"]["procedencia"]["contradice"] = True
        html = self.render(s)
        self.assertIn("El nombre del bin declara", html)
        self.assertIn("variable l5", html)

    def test_sin_contradiccion_no_hay_aviso(self):
        html = self.render(sesion_mandalorian())
        self.assertNotIn("El nombre del bin declara", html)


class TestEstadosLimite(CardTestCase):

    def test_sesion_sin_datos_no_revienta(self):
        html = self.render({"phase": "created"})
        self.assertIn("Aún sin datos", html)

    def test_compat_error_manda(self):
        s = sesion_mandalorian(compat_warning="Combinación imposible")
        html = self.render(s)
        self.assertIn("Compatibilidad estructural", html)
        self.assertNotIn("Los dos RPU", html)

    def test_colapsada_no_pinta_cuerpo(self):
        html = self.render(sesion_mandalorian(), expandida=False)
        self.assertNotIn("Los dos RPU", html)
        self.assertIn("Validaciones", html)

    def test_ack_pendiente_muestra_los_numeros(self):
        s = sesion_mandalorian(awaiting_critical_ack=True)
        html = self.render(s)
        self.assertIn("Esperando tu confirmación", html)
        self.assertIn("278 frames", html)      # el mayor tramo, en la decisión
        self.assertIn("593 de 190.021", html)


class TestEscapado(CardTestCase):

    def test_ningun_dato_se_inyecta_sin_escapar(self):
        veneno = '<script>alert(1)</script>'
        s = sesion_mandalorian(trust_override=veneno, output_workflow=veneno)
        s["sheet_recommendation"]["rows"][0]["notes"] = veneno
        s["target_dv_info"]["l9_primaries"] = veneno
        html = self.render(s)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert", html)


class TestDiagnosticoEnTexto(CardTestCase):

    def test_vuelca_los_bloques(self):
        txt = self.diagnostico(sesion_mandalorian())
        for trozo in ("① Trusted", "② RPU", "③ Gates", "④ Desglose L5",
                      "⑤ Evidencia", "00:10:11", "restore_dropin"):
            self.assertIn(trozo, txt, f"falta «{trozo}» en el diagnóstico")

class TestLaColumnaDeDiferencias(CardTestCase):
    """La cuarta columna del bloque ②, que el usuario leyó y no entendió.

    Tres defectos a la vez, vistos en la card de un job real el 2026-09-23:
    la cabecera decía `¿=?`; la fila «Niveles» anunciaba `+L8` aunque el bin
    trajera además L3 y L9, porque la marca estaba CLAVADA a L8; y las filas
    de L9 y L11 decían `—` en los dos lados mientras «Niveles» afirmaba que
    el bin trae L9 — la tabla se contradecía consigo misma.

    El L9 es el más instructivo: el pipeline de CMv4.0 **no rellena nunca**
    `l9_primaries` ni `l11_content_type` (solo lo hacen Tab 1 y Tab 2), así
    que esas dos filas no podían enseñar nada en esta pestaña. Un guion ahí
    no es «no lo tiene»: es «no lo hemos mirado».
    """

    def test_la_cabecera_se_entiende(self):
        html = self.render(sesion_mandalorian())
        self.assertIn("Diferencias", html)
        self.assertNotIn("¿=?", html)

    def test_los_niveles_que_añade_el_bin_se_listan_todos(self):
        s = sesion_mandalorian()
        s["target_dv_info"] = {**s["target_dv_info"], "has_l3": True}
        fila = _fila_de(self.render(s), "Niveles")
        self.assertIn("+L3", fila)
        self.assertIn("+L8", fila)

    def test_y_un_nivel_que_se_PIERDE_también(self):
        """La resta va en los dos sentidos: que el bin traiga menos que el
        disco es justo lo que hay que ver, y en ámbar."""
        s = sesion_mandalorian()
        s["source_dv_info"] = {**s["source_dv_info"], "has_l11": True}
        fila = _fila_de(self.render(s), "Niveles")
        self.assertIn("−L11", fila)
        self.assertIn("orange", fila)

    def test_l9_no_contradice_a_la_fila_de_niveles(self):
        """`has_l9` está en los dos lados de la fixture y el disco no trae
        el valor: la fila tiene que decir «presente», no «—»."""
        fila = _fila_de(self.render(sesion_mandalorian()), "L9 primaries")
        self.assertIn("presente", fila)

    def test_y_no_se_inventa_una_discrepancia_con_lo_que_no_se_midió(self):
        """«presente» contra «DCI-P3 D65» no son dos valores distintos: es
        un valor y un hueco."""
        fila = _fila_de(self.render(sesion_mandalorian()), "L9 primaries")
        self.assertNotIn("≠", fila)

    def test_un_nivel_realmente_ausente_sigue_siendo_un_guion(self):
        s = sesion_mandalorian()
        s["source_dv_info"] = {**s["source_dv_info"], "has_l9": False}
        fila = _fila_de(self.render(s), "L9 primaries")
        self.assertIn("—", fila)
        self.assertNotIn("presente", fila.split("DCI-P3")[0])



if __name__ == "__main__":
    unittest.main()

class TestNingunIconoSaleComoTexto(CardTestCase):
    """El SVG escapado es visible y no da ningún error.

    `icono()` devuelve HTML; si el valor cae en un campo que el render pasa
    por `escHtml` —razonable, porque ahí van datos del RPU— lo que se pinta es
    `<svg viewBox="0 0 24 24" fill="none" stroke="cu…`. Pasó en la columna
    «¿=?» de la tabla de comparación del bloque ②.

    Se comprueba sobre el HTML RENDERIZADO y no leyendo el fuente: así da
    igual por qué patrón llegue —una variable, un campo de objeto, un
    argumento—, que el síntoma es siempre el mismo.
    """

    def test_ni_en_la_card_completa(self):
        for nombre, sesion in (
                ("mandalorian", sesion_mandalorian()),
                ("ack pendiente", sesion_mandalorian(awaiting_critical_ack=True)),
                ("sin datos", sesion_mandalorian(target_dv_info={}, source_dv_info={})),
                ("colapsada", sesion_mandalorian()),
        ):
            with self.subTest(nombre):
                html = self.render(sesion, expandida=(nombre != "colapsada"))
                self.assertNotIn("&lt;svg", html,
                                 "el código del SVG se ve en pantalla")
                self.assertNotIn("viewBox=&quot;", html)
