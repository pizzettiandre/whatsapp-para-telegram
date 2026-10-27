import asyncio
import json
import shutil
import struct
import unicodedata
import zipfile

import pytest
from telethon import errors

import whatsapp_para_telegram as w

CHAT_IOS = (
    "‎[22/09/2023, 10:15:02] Ana: ‎As mensagens são protegidas com a criptografia de ponta a ponta.\n"
    "[22/09/2023, 10:16:40] Ana: bom dia\n"
    "‎[23/09/2023, 08:01:13] Bia: ‎<anexado: 00000001-PHOTO-2023-09-23-08-01-13.jpg>\n"
    "continuação de uma mensagem com várias linhas\n"
    "[05/01/24, 21:30:00] Ana: ‎<anexado: 00000002-Relatório.pdf>\n"
)


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Isola extraidos/ e o histórico numa pasta temporária."""
    monkeypatch.setattr(w, "PASTA_EXTRACAO", tmp_path / "extraidos")
    monkeypatch.setattr(w, "HISTORICO", tmp_path / "historico_importacoes.json")
    return tmp_path


def escrever_zip_whatsapp(caminho, arquivos):
    """Grava um zip como o WhatsApp: nomes em UTF-8 sem a flag 0x800."""
    original = zipfile.ZipInfo._encodeFilenameFlags
    zipfile.ZipInfo._encodeFilenameFlags = lambda self: (self.filename.encode("utf-8"), self.flag_bits)
    try:
        with zipfile.ZipFile(caminho, "w") as z:
            for nome, dados in arquivos.items():
                z.writestr(zipfile.ZipInfo(nome), dados)
    finally:
        zipfile.ZipInfo._encodeFilenameFlags = original
    return caminho


def truncar_indice(caminho, manter):
    """Reescreve o diretório central deixando só as `manter` primeiras entradas,
    como nos exports do WhatsApp acima de 4 GB."""
    dados = caminho.read_bytes()
    with zipfile.ZipFile(caminho) as z:
        inicio_cd = z.start_dir
    eocd = dados.rfind(b"PK\x05\x06")
    cd = dados[inicio_cd:eocd]
    pos = 0
    for _ in range(manter):
        nl, el, cl = struct.unpack("<HHH", cd[pos + 28:pos + 34])
        pos += 46 + nl + el + cl
    novo_cd = cd[:pos]
    novo_eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, manter, manter,
                            len(novo_cd), inicio_cd, 0)
    caminho.write_bytes(dados[:inicio_cd] + novo_cd + novo_eocd)


# ---------------------------------------------------------------- utilidades
@pytest.mark.parametrize("n, esperado", [(0, "0.0 B"), (1536, "1.5 KB"), (5 * 1024**3, "5.0 GB")])
def test_fmt_bytes(n, esperado):
    assert w.fmt_bytes(n) == esperado


@pytest.mark.parametrize("s, esperado", [(59, "0m59s"), (61, "1m01s"), (3 * 3600 + 120, "3h02m")])
def test_fmt_tempo(s, esperado):
    assert w.fmt_tempo(s) == esperado


# --------------------------------------------------------- leitura do export
def test_periodo_export_ios(tmp_path):
    chat = tmp_path / "_chat.txt"
    chat.write_text(CHAT_IOS, encoding="utf-8")
    assert w.periodo_export(chat) == ("2023-09-22", "2024-01-05", 4)


def test_periodo_export_android(tmp_path):
    chat = tmp_path / "Conversa do WhatsApp com Ana.txt"
    chat.write_text("27/10/2026 09:52 - Ana: oi\n28/10/2026 10:00 - Bia: oi\n", encoding="utf-8")
    assert w.periodo_export(chat) == ("2026-10-27", "2026-10-28", 2)


def test_periodo_export_sem_mensagens(tmp_path):
    chat = tmp_path / "_chat.txt"
    chat.write_text("nada aqui\n", encoding="utf-8")
    assert w.periodo_export(chat) == (None, None, 0)


def test_achar_arquivo_chat_pelo_nome(tmp_path):
    (tmp_path / "_chat.txt").write_text(CHAT_IOS, encoding="utf-8")
    (tmp_path / "notas.txt").write_text(CHAT_IOS, encoding="utf-8")
    todos = list(tmp_path.iterdir())
    assert w.achar_arquivo_chat(tmp_path, todos).name == "_chat.txt"


def test_achar_arquivo_chat_pelo_conteudo(tmp_path):
    (tmp_path / "lista.txt").write_text("compras\nleite\n", encoding="utf-8")
    (tmp_path / "exportado.txt").write_text(CHAT_IOS, encoding="utf-8")
    todos = list(tmp_path.iterdir())
    assert w.achar_arquivo_chat(tmp_path, todos).name == "exportado.txt"


def test_achar_arquivo_chat_sem_txt(tmp_path):
    (tmp_path / "foto.jpg").write_bytes(b"x")
    with pytest.raises(SystemExit):
        w.achar_arquivo_chat(tmp_path, list(tmp_path.iterdir()))


def test_listar_midias_ignora_lixo(tmp_path):
    chat = tmp_path / "_chat.txt"
    chat.write_text(CHAT_IOS, encoding="utf-8")
    for nome in ["a.jpg", ".DS_Store", ".escondido", ".extracao_completa"]:
        (tmp_path / nome).write_bytes(b"x")
    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / "__MACOSX" / "a.jpg").write_bytes(b"x")
    assert [p.name for p in w.listar_midias(tmp_path, chat)] == ["a.jpg"]


def test_conferir_anexos_completo(tmp_path, capsys):
    chat = tmp_path / "_chat.txt"
    chat.write_text(CHAT_IOS, encoding="utf-8")
    # nome em NFD (como o macOS grava) deve casar com o texto em NFC
    midias = [tmp_path / "00000001-PHOTO-2023-09-23-08-01-13.jpg",
              tmp_path / unicodedata.normalize("NFD", "00000002-Relatório.pdf")]
    w.conferir_anexos(chat, midias)
    assert capsys.readouterr().out == ""


def test_conferir_anexos_faltando(tmp_path, capsys):
    chat = tmp_path / "_chat.txt"
    chat.write_text(CHAT_IOS, encoding="utf-8")
    w.conferir_anexos(chat, [tmp_path / "00000001-PHOTO-2023-09-23-08-01-13.jpg"])
    saida = capsys.readouterr().out
    assert "1 anexo(s)" in saida and "00000002-Relatório.pdf" in saida


# ------------------------------------------------------------- descompactação
def test_preparar_pasta_aceita_pasta(tmp_path):
    assert w.preparar_pasta(tmp_path) == tmp_path


def test_preparar_pasta_rejeita_outro_formato(tmp_path):
    arq = tmp_path / "conversa.rar"
    arq.write_bytes(b"x")
    with pytest.raises(SystemExit):
        w.preparar_pasta(arq)


def test_preparar_pasta_zipfile_corrige_nomes_utf8(base, monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda _: None)
    zp = escrever_zip_whatsapp(base / "Conversa.zip", {"_chat.txt": CHAT_IOS, "00000002-Relatório.pdf": b"pdf"})
    pasta = w.preparar_pasta(zp)
    assert pasta == base / "extraidos" / "Conversa"
    nomes = {unicodedata.normalize("NFC", p.name) for p in pasta.iterdir()}
    assert "00000002-Relatório.pdf" in nomes


def test_preparar_pasta_reaproveita_extracao(base, monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda _: None)
    zp = escrever_zip_whatsapp(base / "Conversa.zip", {"_chat.txt": CHAT_IOS})
    pasta = w.preparar_pasta(zp)
    (pasta / "_chat.txt").write_text("alterado", encoding="utf-8")
    assert w.preparar_pasta(zp) == pasta
    assert (pasta / "_chat.txt").read_text(encoding="utf-8") == "alterado"


@pytest.mark.skipif(not shutil.which("bsdtar"), reason="bsdtar não instalado")
def test_preparar_pasta_bsdtar_recupera_indice_truncado(base):
    zp = escrever_zip_whatsapp(base / "Grupo.zip", {
        "00000001-PHOTO.jpg": b"a" * 100,
        "00000002-Relatório.pdf": b"b" * 100,
        "_chat.txt": CHAT_IOS,  # o WhatsApp grava o .txt por último
    })
    truncar_indice(zp, manter=1)
    with zipfile.ZipFile(zp) as z:
        assert z.namelist() == ["00000001-PHOTO.jpg"]  # o índice só enxerga 1 arquivo
    pasta = w.preparar_pasta(zp)
    nomes = {unicodedata.normalize("NFC", p.name) for p in pasta.iterdir()}
    assert {"00000001-PHOTO.jpg", "00000002-Relatório.pdf", "_chat.txt"} <= nomes


# ---------------------------------------------------------- estado e histórico
def test_estado_ida_e_volta(tmp_path):
    chat = tmp_path / "_chat.txt"
    chat.write_text("x", encoding="utf-8")
    p = w.caminho_estado(chat, 123)
    assert p == w.caminho_estado(chat, 123) != w.caminho_estado(chat, 456)
    assert w.carregar_estado(p) == {}
    w.salvar_estado(p, {"import_id": 1, "enviados": ["a.jpg"]})
    assert w.carregar_estado(p) == {"import_id": 1, "enviados": ["a.jpg"]}


def test_historico_registra_e_detecta_sobreposicao(base):
    assert w.carregar_historico() == []
    w.registrar_importacao({"destino_id": 1, "inicio": "2023-01-01", "fim": "2024-06-30", "importado_em": "2024-07-01"})
    w.registrar_importacao({"destino_id": 2, "inicio": "2020-01-01", "fim": "2026-01-01", "importado_em": "2026-01-02"})
    assert len(json.loads(w.HISTORICO.read_text(encoding="utf-8"))) == 2

    def datas(*a):
        return [h["importado_em"] for h in w.importacoes_sobrepostas(*a)]

    assert datas(1, "2024-06-01", "2024-12-31") == ["2024-07-01"]  # cruza o fim
    assert datas(1, "2022-01-01", "2023-01-01") == ["2024-07-01"]  # toca o início
    assert datas(1, "2024-07-01", "2024-12-31") == []              # logo depois
    assert datas(3, "2023-01-01", "2024-01-01") == []              # outro chat
    assert datas(1, None, None) == []                              # export sem datas


def test_historico_periodo_desconhecido(base):
    w.registrar_importacao({"destino_id": 1, "inicio": None, "fim": "2024-08-11", "importado_em": "2024-08-11"})
    assert len(w.importacoes_sobrepostas(1, "2019-01-01", "2019-02-01")) == 1
    assert w.importacoes_sobrepostas(1, "2024-08-12", "2024-09-01") == []


# ------------------------------------------------------------- retentativas
@pytest.fixture
def sem_espera(monkeypatch):
    esperas = []

    async def dormir(s):
        esperas.append(s)

    monkeypatch.setattr(w.asyncio, "sleep", dormir)
    return esperas


def test_retentativa_recupera_de_falha_de_rede(sem_espera):
    tentativas = []

    async def chamada():
        tentativas.append(1)
        if len(tentativas) < 3:
            raise ConnectionError("caiu")
        return "ok"

    assert asyncio.run(w.com_retentativa("teste", chamada)) == "ok"
    assert len(tentativas) == 3 and sem_espera == [5, 10]


def test_retentativa_desiste_depois_do_limite(sem_espera):
    async def chamada():
        raise ConnectionError("caiu")

    with pytest.raises(ConnectionError):
        asyncio.run(w.com_retentativa("teste", chamada, tentativas=3))


def test_retentativa_respeita_floodwait(sem_espera):
    tentativas = []

    async def chamada():
        tentativas.append(1)
        if len(tentativas) == 1:
            raise errors.FloodWaitError(request=None, capture=42)
        return "ok"

    assert asyncio.run(w.com_retentativa("teste", chamada)) == "ok"
    assert sem_espera == [45]


def test_retentativa_nao_repete_import_id_invalido(sem_espera):
    async def chamada():
        raise errors.ImportIdInvalidError(request=None)

    with pytest.raises(errors.ImportIdInvalidError):
        asyncio.run(w.com_retentativa("teste", chamada))
    assert sem_espera == []
