# Configuração de múltiplos bots na AWS

O worker aceita vários bots publicadores por meio da variável de ambiente `TELEGRAM_BOTS_JSON`.

## Exemplo simples

```env
TELEGRAM_BOTS_JSON={"north":"123456:ABC_TOKEN","marca_b":"987654:XYZ_TOKEN"}
```

## Exemplo com nomes

```env
TELEGRAM_BOTS_JSON={"north":{"token":"123456:ABC_TOKEN","name":"North Finance Bot"},"marca_b":{"token":"987654:XYZ_TOKEN","name":"Marca B Bot"}}
```

As chaves `north` e `marca_b` são as mesmas que o Lovable deve salvar em `telegram_bot_key` para cada automação.

Exemplo de automação:

```json
{
  "telegram_bot_key": "north",
  "buttons": [
    {
      "text": "🔥 ACESSAR AGORA",
      "url": "https://site.com",
      "row": 0,
      "sort_order": 0,
      "enabled": true
    }
  ]
}
```

## Instalação na instância atual

Projeto esperado:

```bash
cd /home/ubuntu/autotelegram/autotelegram
```

Editar o `.env`:

```bash
nano .env
```

Adicionar `TELEGRAM_BOTS_JSON` em UMA única linha.

Depois proteger:

```bash
chmod 600 .env
```

Atualizar código/dependências e reiniciar:

```bash
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart telegram-worker
sudo systemctl status telegram-worker
```

Logs:

```bash
sudo journalctl -u telegram-worker -f
```

O startup deve registrar algo como:

```text
[Bots] 'north' conectado: @northfinance_bot
[Bots] 'marca_b' conectado: @marca_b_bot
[Bots] Pool inicializado: 2/2 conectado(s)
```

## Segurança

- Nunca comitar `.env`.
- Nunca colocar tokens no frontend do Lovable.
- Nunca retornar tokens em APIs públicas.
- Cada bot deve ser administrador do canal de destino com permissão para publicar.
- Se um token for revogado pelo BotFather, atualize o valor na AWS e reinicie o serviço.

## Compatibilidade

O worker ainda aceita o modo legado com um único bot:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_BOT_SESSION_NAME=telegram_button_bot
```

Mas para múltiplos bots use `TELEGRAM_BOTS_JSON`.
