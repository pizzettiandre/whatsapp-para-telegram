#!/usr/bin/env python3
"""
Importa uma conversa exportada do WhatsApp para o Telegram usando a API oficial
de importação (messages.initHistoryImport), a mesma usada pelo app do celular.
As mensagens mantêm data/hora originais e aparecem marcadas como "importadas".

Requisitos:
    pip install telethon
    API ID e API hash gerados em https://my.telegram.org  (API development tools)

Uso rápido:
    python whatsapp_para_telegram.py --listar-chats
    python whatsapp_para_telegram.py --export "Conversa.zip" --destino 123456789 --simular
    python whatsapp_para_telegram.py --export "Conversa.zip" --destino 123456789
    python whatsapp_para_telegram.py --sair

O --export aceita o .zip exportado pelo WhatsApp ou a pasta já descompactada.
O --destino aceita @usuario, telefone (+55...) ou o ID numérico (veja --listar-chats).
Cada importação concluída fica registrada em historico_importacoes.json, e o script
avisa quando um export novo cobre um período já importado no mesmo chat.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from pathlib import Path

try:
    from telethon import TelegramClient, errors, utils
    from telethon.tl import functions, types
except ImportError:
    sys.exit("Telethon não está instalado. Rode:  pip install telethon")

BASE = Path(__file__).resolve().parent
PASTA_EXTRACAO = BASE / "extraidos"  # fora da Mesa/Documentos, que podem estar no iCloud
HISTORICO = BASE / "historico_importacoes.json"

PHOTO_EXTS = {".jpg", ".jpeg", ".png"}
IGNORAR = {".ds_store", "thumbs.db", "desktop.ini"}
NOMES_CHAT = re.compile(r"^(_chat|whatsapp chat|conversa do whatsapp|chat de whatsapp)", re.I)
LINHA_WA = re.compile(r"^\[?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4},? \d{1,2}:\d{2}")
DATA_WA = re.compile(r"^\[?(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4}),? \d{1,2}:\d{2}")
ANEXO = re.compile(r"<(?:anexado|attached|adjunto|allegato|angehängt)\s*:\s*([^<>]+)>", re.I)
PHOTO_MAX = 10 * 1024 * 1024  # fotos maiores que isso vão como arquivo

mimetypes.add_type("audio/ogg", ".opus")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("video/mp4", ".mp4")


# ----------------------------------------------------------------- utilidades
def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_tempo(s: float) -> str:
    s = int(s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def preparar_pasta(export: Path) -> Path:
    """Devolve a pasta com os arquivos do export (descompacta o .zip em extraidos/ se preciso)."""
    if export.is_dir():
        return export
    if export.suffix.lower() != ".zip":
        sys.exit(f"--export deve ser um .zip ou uma pasta: {export}")
    destino = PASTA_EXTRACAO / export.stem
    marcador = destino / ".extracao_completa"
    if marcador.exists():
        print(f"Usando pasta já extraída: {destino}")
        return destino
    print(f"Descompactando {export.name} em {destino} ...")
    destino.mkdir(parents=True, exist_ok=True)
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        # Lê o zip em sequência, sem depender do índice central: exports do WhatsApp
        # acima de 4 GB costumam vir com o índice truncado (faltam arquivos, às vezes o .txt).
        # Precisa ser por pipe: com o arquivo direto no stdin o bsdtar volta a usar o índice.
        proc = subprocess.Popen([bsdtar, "-xf", "-", "-C", str(destino)], stdin=subprocess.PIPE)
        try:
            with open(export, "rb") as f:
                shutil.copyfileobj(f, proc.stdin, 8 * 1024 * 1024)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        if proc.wait():
            sys.exit(f"bsdtar falhou ao descompactar {export.name} (código {proc.returncode})")
    else:
        print("⚠ bsdtar não encontrado; usando zipfile, que perde arquivos em zips "
              "do WhatsApp acima de 4 GB")
        with zipfile.ZipFile(export) as z:
            for info in z.infolist():
                # O WhatsApp grava nomes em UTF-8 sem marcar a flag; o zipfile os lê como cp437
                if not info.flag_bits & 0x800:
                    info.filename = info.filename.encode("cp437").decode("utf-8", "replace")
                z.extract(info, destino)
    marcador.touch()
    return destino


def achar_arquivo_chat(pasta: Path, arquivos: list[Path]) -> Path:
    txts = [p for p in arquivos if p.suffix.lower() == ".txt"]
    if not txts:
        sys.exit("Nenhum .txt encontrado no export — é mesmo uma exportação do WhatsApp?")
    por_nome = [p for p in txts if NOMES_CHAT.match(p.name)]
    if len(por_nome) == 1:
        return por_nome[0]

    def pontuacao(p: Path) -> int:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return sum(1 for _, linha in zip(range(200), f) if LINHA_WA.match(linha.lstrip("‎")))

    melhor = max(por_nome or txts, key=pontuacao)
    if pontuacao(melhor) == 0:
        sys.exit("Não consegui identificar o .txt da conversa. Use --arquivo-chat para indicar.")
    return melhor


def listar_midias(pasta: Path, chat_txt: Path) -> list[Path]:
    midias = []
    for p in sorted(pasta.rglob("*")):
        if not p.is_file() or p == chat_txt:
            continue
        if p.name.lower() in IGNORAR or p.name.startswith(".") or "__MACOSX" in p.parts:
            continue
        midias.append(p)
    return midias


def conferir_anexos(chat_txt: Path, midias: list[Path]) -> None:
    """Avisa se a conversa cita anexos que não estão na pasta (extração incompleta)."""
    nomes = {unicodedata.normalize("NFC", p.name) for p in midias}
    with open(chat_txt, encoding="utf-8", errors="ignore") as f:
        citados = {unicodedata.normalize("NFC", m.group(1).strip())
                   for linha in f for m in ANEXO.finditer(linha)}
    faltando = sorted(citados - nomes)
    if faltando:
        print(f"⚠ {len(faltando)} anexo(s) citados na conversa não estão na pasta, "
              f"ex.: {', '.join(faltando[:3])}")


def periodo_export(chat_txt: Path) -> tuple:
    """(primeira data, última data, nº de mensagens). Datas em ISO, assumindo dia/mês/ano."""
    datas = []
    with open(chat_txt, encoding="utf-8", errors="ignore") as f:
        for linha in f:
            m = DATA_WA.match(linha.lstrip("‎"))
            if m:
                datas.append(m.groups())
    if not datas:
        return None, None, 0

    def iso(d, m, a):
        a = int(a) + (2000 if len(a) == 2 else 0)
        return f"{a:04d}-{int(m):02d}-{int(d):02d}"

    return iso(*datas[0]), iso(*datas[-1]), len(datas)


def carregar_historico() -> list:
    if HISTORICO.exists():
        return json.loads(HISTORICO.read_text(encoding="utf-8"))
    return []


def importacoes_sobrepostas(peer_id: int, inicio: str, fim: str) -> list:
    """Importações anteriores no mesmo chat cujo período cruza com [inicio, fim]."""
    if not inicio:
        return []
    return [h for h in carregar_historico()
            if h["destino_id"] == peer_id
            and (not h.get("inicio") or h["inicio"] <= fim)
            and inicio <= h["fim"]]


def registrar_importacao(registro: dict) -> None:
    historico = carregar_historico() + [registro]
    tmp = HISTORICO.with_suffix(".tmp")
    tmp.write_text(json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, HISTORICO)


def caminho_estado(chat_txt: Path, peer_id: int) -> Path:
    h = hashlib.sha1(f"{chat_txt.resolve()}|{peer_id}".encode()).hexdigest()[:10]
    return chat_txt.parent / f".importacao_telegram_{h}.json"


def carregar_estado(p: Path) -> dict:
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def salvar_estado(p: Path, estado: dict) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------- chamadas resilientes
async def com_retentativa(descricao: str, coro_factory, tentativas: int = 6):
    """Executa a chamada tratando FloodWait e falhas de rede com backoff."""
    espera = 5
    for i in range(1, tentativas + 1):
        try:
            return await coro_factory()
        except errors.FloodWaitError as e:
            print(f"  ⏳ FloodWait em '{descricao}': aguardando {e.seconds}s (limite do Telegram)")
            await asyncio.sleep(e.seconds + 3)
        except (errors.ImportIdInvalidError, errors.PreviousChatImportActiveWaitMinError):
            raise
        except (ConnectionError, asyncio.TimeoutError, OSError, errors.ServerError,
                errors.RpcCallFailError, errors.TimedOutError) as e:
            if i == tentativas:
                raise
            print(f"  ⚠ Falha temporária em '{descricao}' ({type(e).__name__}: {e}). "
                  f"Tentativa {i}/{tentativas}, nova tentativa em {espera}s")
            await asyncio.sleep(espera)
            espera = min(espera * 2, 300)


async def montar_media(client, arquivo: Path, como_documento: bool):
    nome_upload = arquivo.name
    tamanho = arquivo.stat().st_size
    ultimo = [0.0]

    def progresso(enviado, total):
        agora = time.monotonic()
        if total > 50 * 1024 * 1024 and agora - ultimo[0] > 15:
            ultimo[0] = agora
            print(f"     … {arquivo.name}: {enviado * 100 // total}% de {fmt_bytes(total)}")

    entrada = await client.upload_file(str(arquivo), file_name=nome_upload,
                                       progress_callback=progresso)
    ext = arquivo.suffix.lower()
    if not como_documento and ext in PHOTO_EXTS and tamanho <= PHOTO_MAX:
        return types.InputMediaUploadedPhoto(file=entrada)
    mime = mimetypes.guess_type(arquivo.name)[0] or "application/octet-stream"
    return types.InputMediaUploadedDocument(
        file=entrada,
        mime_type=mime,
        attributes=[types.DocumentAttributeFilename(file_name=arquivo.name)],
    )


async def enviar_midia(client, peer, import_id: int, arquivo: Path):
    try:
        media = await com_retentativa(f"upload {arquivo.name}",
                                      lambda: montar_media(client, arquivo, False))
        return await com_retentativa(
            f"registrar {arquivo.name}",
            lambda: client(functions.messages.UploadImportedMediaRequest(
                peer=peer, import_id=import_id, file_name=arquivo.name, media=media)))
    except errors.RPCError as e:
        # Foto recusada (dimensão/tamanho/formato): reenvia como arquivo
        if "PHOTO" in str(e).upper() or "IMAGE" in str(e).upper():
            print(f"  ↻ {arquivo.name} recusado como foto ({e}); reenviando como arquivo")
            media = await com_retentativa(f"upload {arquivo.name}",
                                          lambda: montar_media(client, arquivo, True))
            return await com_retentativa(
                f"registrar {arquivo.name}",
                lambda: client(functions.messages.UploadImportedMediaRequest(
                    peer=peer, import_id=import_id, file_name=arquivo.name, media=media)))
        raise


# ----------------------------------------------------------------------- main
async def listar_chats(client):
    print(f"{'ID':>16}  {'TIPO':<10} NOME")
    async for d in client.iter_dialogs(limit=200):
        e = d.entity
        if isinstance(e, types.User):
            tipo = "bot" if e.bot else ("contato" if e.mutual_contact else "usuário")
        elif isinstance(e, types.Chat):
            tipo = "grupo"
        elif getattr(e, "megagroup", False):
            tipo = "supergrupo"
        else:
            tipo = "canal"
        print(f"{d.id:>16}  {tipo:<10} {d.name}")


async def resolver_destino(client, destino: str):
    await client.get_dialogs(limit=None)  # popula o cache para IDs numéricos
    alvo = int(destino) if re.fullmatch(r"-?\d+", destino) else destino
    entidade = await client.get_entity(alvo)
    return entidade, utils.get_input_peer(entidade)


async def importar(args):
    export = Path(args.export).expanduser()
    pasta = preparar_pasta(export)
    todos = [p for p in pasta.rglob("*") if p.is_file()]
    chat_txt = Path(args.arquivo_chat) if args.arquivo_chat else achar_arquivo_chat(pasta, todos)
    midias = listar_midias(pasta, chat_txt)
    total_bytes = sum(p.stat().st_size for p in midias)

    inicio, fim, n_msgs = periodo_export(chat_txt)

    print(f"\nArquivo da conversa: {chat_txt.name} ({fmt_bytes(chat_txt.stat().st_size)})")
    print(f"Período: {inicio} → {fim} ({n_msgs} mensagens)")
    print(f"Mídias: {len(midias)} arquivos, {fmt_bytes(total_bytes)}")
    conferir_anexos(chat_txt, midias)
    grandes = [p for p in midias if p.stat().st_size > 2 * 1024**3]
    if grandes:
        print("⚠ Arquivos acima de 2 GB (só funcionam com Telegram Premium, até 4 GB):")
        for p in grandes:
            print(f"   - {p.name} ({fmt_bytes(p.stat().st_size)})")

    async with TelegramClient(args.sessao, args.api_id, args.api_hash) as client:
        # 1) Valida o formato do export
        with open(chat_txt, encoding="utf-8", errors="ignore") as f:
            cabecalho = "".join(linha for _, linha in zip(range(100), f))
        parsed = await com_retentativa("checar export", lambda: client(
            functions.messages.CheckHistoryImportRequest(import_head=cabecalho)))
        tipo = "conversa privada" if parsed.pm else ("grupo" if parsed.group else "desconhecido")
        print(f"Telegram reconheceu o export como: {tipo}"
              + (f" — título '{parsed.title}'" if parsed.title else ""))

        # 2) Valida o destino
        entidade, peer = await resolver_destino(client, args.destino)
        nome = utils.get_display_name(entidade)
        if isinstance(entidade, types.Chat):
            print(f"\n'{nome}' é um grupo básico; a importação exige supergrupo.")
            if not args.migrar_grupo:
                sys.exit("Rode de novo com --migrar-grupo para converter (o ID do grupo muda), "
                         "ou converta pelo app.")
            upd = await client(functions.messages.MigrateChatRequest(chat_id=entidade.id))
            novo = next(c for c in upd.chats if isinstance(c, types.Channel))
            entidade, peer = novo, utils.get_input_peer(novo)
            print(f"Convertido para supergrupo. Novo ID: {utils.get_peer_id(novo)}")

        if parsed.pm and not isinstance(entidade, types.User):
            print("⚠ O export é de conversa privada, mas o destino não é um usuário.")
        if parsed.group and isinstance(entidade, types.User):
            print("⚠ O export é de grupo, mas o destino é um usuário.")

        checado = await com_retentativa("checar destino", lambda: client(
            functions.messages.CheckHistoryImportPeerRequest(peer=peer)))
        print(f"\nDestino: {nome}\nTelegram diz: {checado.confirm_text}")

        peer_id = utils.get_peer_id(entidade)
        for h in importacoes_sobrepostas(peer_id, inicio, fim):
            print(f"\n⚠ ATENÇÃO: em {h['importado_em']} já foi importado neste chat o período "
                  f"{h.get('inicio') or '?'} → {h['fim']} ({h.get('mensagens', '?')} mensagens). "
                  "Importar de novo vai duplicar essas mensagens.")

        if args.simular:
            print("\n✔ Simulação concluída: export e destino aceitos. Nada foi importado.")
            return

        estado_p = caminho_estado(chat_txt, peer_id)
        estado = carregar_estado(estado_p)
        if estado.get("concluido"):
            sys.exit(f"Esta importação já foi concluída antes (registro em {estado_p.name}).")

        resposta = input("\nDigite 'sim' para iniciar a importação: ").strip().lower()
        if resposta != "sim":
            sys.exit("Cancelado.")

        # 3) Inicia (ou retoma) a importação
        import_id = estado.get("import_id")
        enviados = set(estado.get("enviados", []))
        if import_id:
            print(f"Retomando importação {import_id} ({len(enviados)}/{len(midias)} mídias já enviadas)")
        else:
            arquivo_chat = await client.upload_file(str(chat_txt), file_name=chat_txt.name)
            try:
                res = await com_retentativa("iniciar importação", lambda: client(
                    functions.messages.InitHistoryImportRequest(
                        peer=peer, file=arquivo_chat, media_count=len(midias))))
            except errors.PreviousChatImportActiveWaitMinError as e:
                sys.exit(f"Já houve uma importação recente neste chat. O Telegram pede "
                         f"para aguardar {e.minutes} minuto(s) antes da próxima.")
            import_id, enviados = res.id, set()
            estado = {"import_id": import_id, "enviados": [], "destino": nome,
                      "media_count": len(midias)}
            salvar_estado(estado_p, estado)
            print(f"Importação iniciada (id {import_id}).")

        # 4) Envia as mídias em paralelo, salvando o progresso a cada arquivo
        pendentes = [p for p in midias if str(p.relative_to(pasta)) not in enviados]
        bytes_pendentes = sum(p.stat().st_size for p in pendentes)
        sem = asyncio.Semaphore(args.paralelo)
        feito = {"n": 0, "bytes": 0}
        inicio = time.monotonic()
        falhas = []

        async def worker(p: Path):
            async with sem:
                try:
                    await enviar_midia(client, peer, import_id, p)
                except errors.ImportIdInvalidError:
                    raise
                except Exception as e:  # noqa: BLE001
                    falhas.append((p.name, f"{type(e).__name__}: {e}"))
                    print(f"  ✖ {p.name}: {type(e).__name__}: {e}")
                    return
                enviados.add(str(p.relative_to(pasta)))
                estado["enviados"] = sorted(enviados)
                salvar_estado(estado_p, estado)
                feito["n"] += 1
                feito["bytes"] += p.stat().st_size
                dec = time.monotonic() - inicio
                vel = feito["bytes"] / dec if dec else 0
                eta = (bytes_pendentes - feito["bytes"]) / vel if vel else 0
                print(f"  ✔ [{len(enviados)}/{len(midias)}] {p.name} "
                      f"— {fmt_bytes(vel)}/s, faltam ~{fmt_tempo(eta)}")

        try:
            await asyncio.gather(*(worker(p) for p in pendentes))
        except errors.ImportIdInvalidError:
            estado_p.unlink(missing_ok=True)
            sys.exit("O Telegram expirou esta importação. Rode o script de novo para recomeçar do zero.")

        if falhas:
            print(f"\n{len(falhas)} mídia(s) falharam. O progresso está salvo; rode o mesmo "
                  "comando de novo para tentar só as que faltam.")
            sys.exit(1)

        # 5) Finaliza
        await com_retentativa("finalizar importação", lambda: client(
            functions.messages.StartHistoryImportRequest(peer=peer, import_id=import_id)))
        estado["concluido"] = True
        salvar_estado(estado_p, estado)
        registrar_importacao({"destino_id": peer_id, "destino": nome, "inicio": inicio, "fim": fim,
                              "mensagens": n_msgs, "midias": len(midias),
                              "importado_em": time.strftime("%Y-%m-%d"), "export": export.name})
        print("\n✅ Importação enviada! O Telegram processa no servidor; as mensagens podem "
              "levar alguns minutos para aparecer no chat.")


def main():
    ap = argparse.ArgumentParser(description="Importa export do WhatsApp para o Telegram.")
    ap.add_argument("--export", help="arquivo .zip exportado do WhatsApp ou pasta descompactada")
    ap.add_argument("--destino", help="@usuario, +telefone ou ID numérico do chat de destino")
    ap.add_argument("--arquivo-chat", help="caminho do .txt da conversa (se a detecção falhar)")
    ap.add_argument("--listar-chats", action="store_true", help="lista seus chats e IDs e sai")
    ap.add_argument("--simular", action="store_true", help="só valida export e destino, sem importar")
    ap.add_argument("--migrar-grupo", action="store_true",
                    help="converte grupo básico em supergrupo (exigido pela importação)")
    ap.add_argument("--paralelo", type=int, default=3, help="uploads simultâneos (padrão 3)")
    ap.add_argument("--sair", action="store_true",
                    help="encerra a sessão no Telegram e apaga o arquivo de sessão local")
    ap.add_argument("--sessao", default=str(BASE / "importador_wa"),
                    help="arquivo de sessão local (padrão: ao lado do script)")
    ap.add_argument("--api-id", type=int, default=os.getenv("TG_API_ID"))
    ap.add_argument("--api-hash", default=os.getenv("TG_API_HASH"))
    args = ap.parse_args()

    if args.sair and not Path(args.sessao + ".session").exists():
        sys.exit("Nenhuma sessão salva; nada a encerrar.")

    if not args.api_id:
        args.api_id = int(input("API ID (my.telegram.org): ").strip())
    if not args.api_hash:
        args.api_hash = getpass.getpass("API hash (não aparece ao digitar): ").strip()

    if args.listar_chats:
        async def _l():
            async with TelegramClient(args.sessao, args.api_id, args.api_hash) as c:
                await listar_chats(c)
        asyncio.run(_l())
        return

    if args.sair:
        async def _s():
            # connect() em vez de "async with": não pede login só para sair
            c = TelegramClient(args.sessao, args.api_id, args.api_hash)
            await c.connect()
            if await c.is_user_authorized():
                await c.log_out()  # invalida no servidor e apaga o .session
            else:
                await c.disconnect()
                Path(args.sessao + ".session").unlink(missing_ok=True)
        asyncio.run(_s())
        print("Sessão encerrada no Telegram e arquivo de sessão apagado.")
        return

    if not args.export or not args.destino:
        ap.error("informe --export e --destino (ou use --listar-chats)")
    asyncio.run(importar(args))


if __name__ == "__main__":
    main()
