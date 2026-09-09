# Multi-sessão Telegram na AWS

Arquitetura recomendada: um processo systemd isolado por conta Telegram, usando o mesmo repositório/venv.

Cada automação escolhe a conta de leitura por `telegram_session_key`.

## Exemplo

- Conta principal: key `primary`, session name `telegram_main`
- Conta secundária: key `marca_b`, session name `telegram_marca_b`

A sessão `primary` pode ser marcada como default para continuar processando automações antigas que ainda não possuem `telegram_session_key`.

## Estrutura de arquivos

```text
/home/ubuntu/autotelegram/autotelegram/
├── .env                         # segredos/configuração comum
├── worker.py
├── session_worker.py
├── telegram_buttons.py
├── sessions/
│   ├── primary.env
│   └── marca_b.env
├── telegram_main.session
└── telegram_marca_b.session
```

## 1. Criar diretório de configuração das sessões

```bash
cd /home/ubuntu/autotelegram/autotelegram
mkdir -p sessions bot_sessions/primary bot_sessions/marca_b
chmod 700 sessions bot_sessions bot_sessions/primary bot_sessions/marca_b
```

## 2. Sessão principal

Criar:

```bash
nano sessions/primary.env
```

Conteúdo:

```env
TELEGRAM_SESSION_KEY=primary
TELEGRAM_SESSION_NAME=/home/ubuntu/autotelegram/autotelegram/telegram_main
TELEGRAM_SESSION_IS_DEFAULT=1
TELEGRAM_WORKER_ID=telegram-primary
TELEGRAM_BOT_SESSION_DIR=/home/ubuntu/autotelegram/autotelegram/bot_sessions/primary
```

## 3. Segunda conta

Criar:

```bash
nano sessions/marca_b.env
```

Conteúdo:

```env
TELEGRAM_SESSION_KEY=marca_b
TELEGRAM_SESSION_NAME=/home/ubuntu/autotelegram/autotelegram/telegram_marca_b
TELEGRAM_SESSION_IS_DEFAULT=0
TELEGRAM_WORKER_ID=telegram-marca-b
TELEGRAM_BOT_SESSION_DIR=/home/ubuntu/autotelegram/autotelegram/bot_sessions/marca_b
```

Proteja os arquivos:

```bash
chmod 600 sessions/*.env
```

## 4. Instalar template systemd

```bash
sudo cp deploy/telegram-worker@.service /etc/systemd/system/telegram-worker@.service
sudo systemctl daemon-reload
```

## 5. Criar a sessão Telegram da conta nova

Antes de subir o serviço pela primeira vez, gere o arquivo de sessão de forma interativa:

```bash
cd /home/ubuntu/autotelegram/autotelegram
set -a
source .env
source sessions/marca_b.env
set +a
./venv/bin/python -c "import asyncio; from telethon import TelegramClient; import os; async def m():\n c=TelegramClient(os.environ['TELEGRAM_SESSION_NAME'], int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH']); await c.start(); me=await c.get_me(); print('Conectado:', me.id, me.username); await c.disconnect()\n; asyncio.run(m())"
```

Se o shell rejeitar esse comando em uma linha, use o script `create_user_session.py` descrito abaixo ou execute `session_worker.py` manualmente uma vez para concluir o login.

O Telegram pedirá:
- número com DDI, ex. `+55...`;
- código enviado pelo Telegram;
- senha 2FA, se habilitada.

Depois disso o arquivo `telegram_marca_b.session` fica salvo no servidor e o serviço consegue iniciar sem pedir login novamente.

## 6. Ativar serviços

```bash
sudo systemctl enable --now telegram-worker@primary
sudo systemctl enable --now telegram-worker@marca_b
```

Status:

```bash
sudo systemctl status telegram-worker@primary
sudo systemctl status telegram-worker@marca_b
```

Logs:

```bash
sudo journalctl -u telegram-worker@primary -f
sudo journalctl -u telegram-worker@marca_b -f
```

## 7. Contrato da automação

Cada automação deve receber do Lovable:

```json
{
  "telegram_session_key": "marca_b",
  "source_chat_id": "-100111",
  "destination_chat_id": "-100222"
}
```

O processo `marca_b` só processa automações com `telegram_session_key = marca_b`.

O processo `primary`, quando `TELEGRAM_SESSION_IS_DEFAULT=1`, também processa tarefas antigas sem essa propriedade, preservando compatibilidade.

## 8. Vários bots + várias sessões

`TELEGRAM_BOTS_JSON` continua no `.env` comum. Cada processo pode publicar usando qualquer bot selecionado pela automação (`telegram_bot_key`).

Como vários processos usam os mesmos bots, cada sessão recebe um `TELEGRAM_BOT_SESSION_DIR` diferente para evitar que dois processos abram o mesmo arquivo SQLite `.session` do bot.

## 9. Importante

Não use o mesmo `TELEGRAM_SESSION_NAME` para duas contas.
Não use a mesma `TELEGRAM_SESSION_KEY` para dois serviços.
Não deixe duas sessões marcadas como `TELEGRAM_SESSION_IS_DEFAULT=1`.
Não versione `.env`, `sessions/*.env` com segredos nem arquivos `.session`.
