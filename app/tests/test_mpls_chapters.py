"""Parser binario de capítulos del MPLS (_parse_mpls_marks_binary).

Fallback de parse_mpls_chapters para discos UHD multi-segmento donde mkvmerge
aborta al procesar el playlist (caso Avatar Fuego y Ceniza 2025: 57 capítulos
reales que caían a 19 auto-generados cada 10 min). Los capítulos viven en los
PlayListMark del MPLS; el parser los lee sin invocar mkvmerge.

Construimos MPLS sintéticos en memoria (header + PlayList + PlayListMark) para
validar el cálculo de tiempo absoluto multi-segmento sin discos reales:
    abs = Σ(OUT-IN de PlayItems previos) + (mark_ts - IN del PlayItem ref)

Ejecutar desde la raíz del repo (necesita el venv con pydantic):
    .venv/bin/python -m unittest app.tests.test_mpls_chapters -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_a import _parse_mpls_marks_binary, _ticks_to_timestamp  # noqa: E402

HZ = 45000  # ticks por segundo en los timestamps MPLS


def _u16(v: int) -> bytes:
    return int(v).to_bytes(2, "big")


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "big")


def _playitem(in_ticks: int, out_ticks: int) -> bytes:
    """PlayItem mínimo (32 bytes fixed, sin STN ni multi-angle), length-prefixed.
    Layout idéntico al que parsea parse_mpls_pg_streams: IN en +12, OUT en +16."""
    body = bytearray()
    body += b"00000"          # +0  Clip_Information_file_name (5)
    body += b"M2TS"           # +5  Clip_codec_identifier (4)
    body += _u16(0)           # +9  flags16 (is_multi_angle=0)
    body += bytes([0])        # +11 ref_to_STC_id
    body += _u32(in_ticks)    # +12 IN_time
    body += _u32(out_ticks)   # +16 OUT_time
    body += bytes(8)          # +20 UO_mask_table (8 bytes)
    body += bytes([0])        # +28 flags8
    body += bytes([0])        # +29 still_mode
    body += _u16(0)           # +30 still_time
    assert len(body) == 32
    return _u16(len(body)) + bytes(body)


def _mark(mark_type: int, ref_pi: int, ts: int) -> bytes:
    """PlayListMark de 14 bytes."""
    m = bytearray()
    m += bytes([0])           # reserved
    m += bytes([mark_type])   # mark_type (0x01 = entry / capítulo)
    m += _u16(ref_pi)         # ref_to_PlayItem_id
    m += _u32(ts)             # mark_time_stamp
    m += _u16(0)              # entry_ES_PID
    m += _u32(0)              # duration
    assert len(m) == 14
    return bytes(m)


def _build_mpls(playitems: list, marks: list) -> str:
    """Escribe un MPLS sintético a un fichero temporal y devuelve su path.
    playitems: [(in, out), ...]   marks: [(type, ref_pi, ts), ...]"""
    pi_bytes = b"".join(_playitem(i, o) for i, o in playitems)
    playlist = bytearray()
    playlist += _u32(0)              # length (se corrige abajo)
    playlist += _u16(0)              # reserved
    playlist += _u16(len(playitems)) # number_of_PlayItems
    playlist += _u16(0)              # number_of_SubPaths
    playlist += pi_bytes
    playlist[0:4] = _u32(len(playlist) - 4)

    mark_bytes = b"".join(_mark(t, r, ts) for t, r, ts in marks)
    plm = bytearray()
    plm += _u32(0)                   # length (se corrige abajo)
    plm += _u16(len(marks))          # number_of_PlayList_marks
    plm += mark_bytes
    plm[0:4] = _u32(len(plm) - 4)

    header = bytearray(40)
    header[0:4]   = b"MPLS"
    header[4:8]   = b"0200"
    header[8:12]  = _u32(40)                    # PlayList_start_address
    header[12:16] = _u32(40 + len(playlist))    # PlayListMark_start_address
    header[16:20] = _u32(0)                     # ExtensionData_start_address

    blob = bytes(header) + bytes(playlist) + bytes(plm)
    tmp = tempfile.NamedTemporaryFile(suffix=".mpls", delete=False)
    tmp.write(blob)
    tmp.close()
    return tmp.name


class TestTicksToTimestamp(unittest.TestCase):
    def test_cero(self):
        self.assertEqual(_ticks_to_timestamp(0), "00:00:00.000")

    def test_un_segundo(self):
        self.assertEqual(_ticks_to_timestamp(HZ), "00:00:01.000")

    def test_horas_minutos_segundos(self):
        # 1h 01m 01s
        self.assertEqual(_ticks_to_timestamp(HZ * 3661), "01:01:01.000")

    def test_milisegundos(self):
        self.assertEqual(_ticks_to_timestamp(HZ // 2), "00:00:00.500")


class TestMplsMarksBinary(unittest.TestCase):
    def setUp(self):
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            Path(p).unlink(missing_ok=True)

    def _mpls(self, playitems, marks):
        path = _build_mpls(playitems, marks)
        self._paths.append(path)
        return path

    def test_un_solo_segmento(self):
        # PlayItem único 0..600s; marks en 0, 120s, 300s.
        path = self._mpls(
            playitems=[(0, HZ * 600)],
            marks=[
                (0x01, 0, 0),
                (0x01, 0, HZ * 120),
                (0x01, 0, HZ * 300),
            ],
        )
        chapters = _parse_mpls_marks_binary(path)
        self.assertEqual([c["timestamp"] for c in chapters],
                         ["00:00:00.000", "00:02:00.000", "00:05:00.000"])
        self.assertEqual([c["number"] for c in chapters], [1, 2, 3])
        self.assertTrue(all(c["name"].startswith("Capítulo") for c in chapters))
        self.assertTrue(all(c["name_custom"] is False for c in chapters))

    def test_multi_segmento_acumula_offset(self):
        # Caso clave (Avatar): el playlist concatena 2 PlayItems. Los marks del
        # 2º PlayItem deben sumar la duración del 1º (offset acumulado) y restar
        # su propio IN_time.
        #   PI0: IN=0,        OUT=600s   → dur 600s, offset 0
        #   PI1: IN=100s,     OUT=400s   → dur 300s, offset 600s
        path = self._mpls(
            playitems=[(0, HZ * 600), (HZ * 100, HZ * 400)],
            marks=[
                (0x01, 0, 0),                    # 0s
                (0x01, 0, HZ * 300),             # 300s
                (0x01, 1, HZ * 100),             # 600s + (100-100) = 600s
                (0x01, 1, HZ * 250),             # 600s + (250-100) = 750s
            ],
        )
        chapters = _parse_mpls_marks_binary(path)
        self.assertEqual([c["timestamp"] for c in chapters],
                         ["00:00:00.000", "00:05:00.000", "00:10:00.000", "00:12:30.000"])

    def test_ignora_marks_no_entry(self):
        # mark_type 0x02 (link point) no es capítulo → se ignora.
        path = self._mpls(
            playitems=[(0, HZ * 600)],
            marks=[
                (0x01, 0, 0),
                (0x02, 0, HZ * 60),       # link point — ignorado
                (0x01, 0, HZ * 120),
            ],
        )
        chapters = _parse_mpls_marks_binary(path)
        self.assertEqual([c["timestamp"] for c in chapters],
                         ["00:00:00.000", "00:02:00.000"])

    def test_descarta_end_mark_degenerado(self):
        # Mark final pegado al fin (== duración total) = marca de fin del
        # playlist, no un capítulo. Se descarta (caso Avatar: 58 → 57).
        path = self._mpls(
            playitems=[(0, HZ * 600)],
            marks=[
                (0x01, 0, 0),
                (0x01, 0, HZ * 300),
                (0x01, 0, HZ * 600),   # == total → end mark degenerado
            ],
        )
        chapters = _parse_mpls_marks_binary(path)
        self.assertEqual([c["timestamp"] for c in chapters],
                         ["00:00:00.000", "00:05:00.000"])

    def test_no_descarta_capitulo_real_cerca_del_final(self):
        # Un capítulo 3s antes del final (>1s) SÍ es navegable → se mantiene.
        path = self._mpls(
            playitems=[(0, HZ * 600)],
            marks=[
                (0x01, 0, 0),
                (0x01, 0, HZ * 300),
                (0x01, 0, HZ * 597),   # 3s antes del final → capítulo real
            ],
        )
        self.assertEqual(len(_parse_mpls_marks_binary(path)), 3)

    def test_menos_de_dos_marks_devuelve_vacio(self):
        # 1 solo entry mark no es una lista útil → [] (se prefiere auto-generado).
        path = self._mpls(playitems=[(0, HZ * 600)], marks=[(0x01, 0, 0)])
        self.assertEqual(_parse_mpls_marks_binary(path), [])

    def test_timestamp_fuera_de_rango_descarta(self):
        # Un mark cuyo tiempo absoluto excede la duración total >5% → cálculo
        # erróneo (p. ej. multi-ángulo) → [] en vez de capítulos mal colocados.
        path = self._mpls(
            playitems=[(0, HZ * 600)],
            marks=[
                (0x01, 0, 0),
                (0x01, 0, HZ * 40000),    # 40000s >> 600s total (cabe en uint32)
            ],
        )
        self.assertEqual(_parse_mpls_marks_binary(path), [])

    def test_fichero_no_mpls(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".mpls", delete=False)
        tmp.write(b"NOTMPLS" + bytes(100))
        tmp.close()
        self._paths.append(tmp.name)
        self.assertEqual(_parse_mpls_marks_binary(tmp.name), [])

    def test_fichero_inexistente(self):
        self.assertEqual(_parse_mpls_marks_binary("/no/existe/x.mpls"), [])


if __name__ == "__main__":
    unittest.main()
