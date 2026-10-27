# WhatsApp → Telegram

Importa uma conversa exportada do WhatsApp (privada ou de grupo) para o Telegram pela
**API oficial de importação** (`messages.initHistoryImport`), a mesma usada pelo app do
celular. As mensagens mantêm a data e a hora originais e aparecem marcadas como importadas.

Tem duas vantagens sobre importar pelo celular: funciona com exports grandes (vários GB) e
retoma de onde parou se a conexão cair.

## Requisitos

- Python 3.9+
- `bsdtar` (já vem no macOS; no Linux, pacote `libarchive-tools`). Sem ele, o script usa o
  `zipfile` do Python, que perde arquivos em exports do WhatsApp acima de 4 GB (veja abaixo).
- API ID e API hash do Telegram: crie em <https://my.telegram.org> → *API development tools*.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Passo a passo

1. **Exporte a conversa no WhatsApp:** abra a conversa → nome do contato/grupo →
   *Exportar conversa* → *Anexar mídia*. Salve o `.zip` no computador.
2. **Confira o que já está no Telegram.** Se essa conversa já foi importada antes, importar de
   novo duplica as mensagens. O script avisa quando o período já consta no
   `historico_importacoes.json`, mas só conhece as importações feitas por ele. Na dúvida,
   exporte o chat pelo Telegram Desktop (*Configurações → Avançado → Exportar dados*) e compare.
3. **Para grupos:** o destino precisa ser um **supergrupo** com todos os membros já adicionados.
   O script converte um grupo básico com `--migrar-grupo` (o ID muda).
4. **Ache o ID do destino:**
   ```sh
   .venv/bin/python whatsapp_para_telegram.py --listar-chats
   ```
   Na primeira vez ele pede API ID, API hash, telefone e o código de login. O código chega
   no app do Telegram.
5. **Simule** (valida o export e o destino sem importar nada):
   ```sh
   .venv/bin/python whatsapp_para_telegram.py --export "WhatsApp Chat - Fulano.zip" --destino 123456789 --simular
   ```
6. **Importe** (pede confirmação digitando `sim`):
   ```sh
   caffeinate -i .venv/bin/python whatsapp_para_telegram.py --export "WhatsApp Chat - Fulano.zip" --destino 123456789
   ```
   O `caffeinate` (macOS) impede o Mac de dormir durante o upload. Se cair a conexão ou
   alguma mídia falhar, rode o mesmo comando de novo: ele envia só o que falta.
7. **Encerre a sessão** quando terminar:
   ```sh
   .venv/bin/python whatsapp_para_telegram.py --sair
   ```

O API ID e o hash podem vir das variáveis `TG_API_ID` e `TG_API_HASH` em vez de serem
digitados.

## Comportamento do Telegram

- **Onde as mensagens aparecem:** se o chat de destino tiver menos de 1.000 mensagens, as
  importadas se misturam ao histórico na data original. Se tiver mais, entram em bloco no dia
  da importação, cada uma marcada com a data original.
- **Quem vê:** todos os participantes do chat.
- **Não há botão para desfazer** a importação inteira.
- **Limite de tamanho:** arquivos acima de 2 GB só sobem com Telegram Premium (até 4 GB).

## Detalhes técnicos

- **Exports acima de 4 GB:** o WhatsApp grava o índice do zip truncado. Leitores que
  dependem do índice (`unzip`, `zipfile`, Finder) enxergam só parte dos arquivos e
  normalmente perdem o `_chat.txt`, que fica no final. O script descompacta com `bsdtar`
  lendo o zip em sequência, por pipe, e confere se toda mídia citada na conversa
  (`<anexado: …>`) existe na pasta.
- **Nomes com acento:** o WhatsApp grava os nomes em UTF-8 sem marcar a flag do zip, e o
  `zipfile` os leria como cp437. O script corrige isso.
- **Local da extração:** o zip é descompactado em `extraidos/`, ao lado do script, e não ao
  lado do zip. Assim a extração não cai numa pasta sincronizada (Mesa/Documentos no iCloud).
- **Datas:** o período do export é lido assumindo dia/mês/ano.

## Arquivos locais (fora do Git)

| Arquivo | O que é |
|---|---|
| `importador_wa.session` | login ativo na sua conta. Não compartilhe; apague com `--sair`. |
| `historico_importacoes.json` | o que já foi importado e para qual chat |
| `extraidos/` | exports descompactados |
| `.importacao_telegram_*.json` | progresso de uma importação, para retomar |

## Testes

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
