# Lovable — corrigir sync de sessões/bots + estilo dos botões

A infraestrutura AWS/worker foi atualizada. Corrija o Lovable sem recriar a arquitetura existente e sem quebrar automações atuais.

## 1. Corrigir lista de Contas Telegram

Problema atual: a sessão secundária pode estar conectada no AWS, mas o seletor da automação mostra somente `primary`.

O heartbeat do worker passa a enviar também:

```json
{
  "telegram_session_key": "marca_b",
  "telegram_session_is_default": false,
  "worker_id": "telegram-main-marca_b",
  "telegram_user_id": "123456",
  "telegram_username": "usuario_b"
}
```

Ao receber heartbeat, faça UPSERT da conta em `telegram_reader_accounts` usando `telegram_session_key` como chave lógica única.

- Se `key` não existir, criar registro automaticamente.
- Se existir, atualizar telegram_user_id, telegram_username, status e last_seen_at.
- `enabled` deve ser true por padrão para nova conta descoberta.
- Não sobrescrever nome amigável customizado pelo usuário; só gerar nome inicial se estiver vazio, por exemplo `Conta marca_b`.
- `primary` continua sendo fallback para automações antigas.

O endpoint de sincronização de chats agora também recebe `telegram_session_key` no payload e em cada chat. Persistir a associação session + chat e nunca sobrescrever chats de outra sessão.

O seletor na automação deve carregar TODAS as contas ativas, não somente `primary` ou a conta default.

Renomear o label visual:

`Conta Telegram de origem` -> `Conta Telegram`

Não renomear o campo técnico `telegram_session_key`.

## 2. Corrigir lista de bots publicadores

O heartbeat do worker passa a enviar um array PÚBLICO:

```json
"publisher_bots": [
  {
    "key": "north",
    "name": "North Finance Bot",
    "telegram_id": "123456789",
    "username": "northfinance_bot",
    "connected": true
  }
]
```

Esse array NUNCA contém token.

Ao receber heartbeat:
- fazer UPSERT em `telegram_publisher_bots` pela `key`;
- criar automaticamente bot ainda inexistente;
- atualizar `name`, `telegram_bot_id`, `telegram_username`, status/connected e last_seen_at quando existirem esses campos;
- `enabled` true por padrão para bot novo conectado;
- nunca criar, pedir, armazenar ou devolver token;
- não apagar bots ausentes em um heartbeat isolado; apenas atualizar os recebidos.

O seletor `Bot publicador` da automação deve listar bots ativos/enableados descobertos pelo worker.

Se não houver `publisher_bots`, mostrar estado vazio útil: `Nenhum bot conectado no worker AWS`.

## 3. Botões com cor/estilo Telegram

O Telegram suporta estilos predefinidos de botão. Não usar HEX/RGB arbitrário.

Adicionar campo lógico `style` em cada botão com os valores:

- `default` ou null = padrão do Telegram
- `primary` = azul
- `success` = verde
- `danger` = vermelho

Na UI mostrar:

Cor do botão
- Padrão
- Azul
- Verde
- Vermelho

Persistir internamente:

```json
{
  "text": "🔥 ACESSAR AGORA",
  "url": "https://site.com",
  "row": 0,
  "sort_order": 0,
  "enabled": true,
  "style": "success"
}
```

Se usar `automation_buttons`, adicionar coluna nullable `style` sem migração destrutiva. Validar apenas `primary`, `success`, `danger`, `default`/null.

O endpoint `GET /api/public/worker/automations` deve devolver `style` em cada botão.

Para registros antigos sem style, retornar `style: null` ou `style: "default"`.

## 4. Regras do formulário

Na tela Nova/Editar Automação manter a ordem aproximada:

- Nome
- Conta Telegram
- Canal/Grupo de origem
- Canal/Grupo de destino
- Bot publicador
- Replacements
- Blacklist
- opções de mídia/caption/formatação
- Botões da mensagem

Ao selecionar Conta Telegram, filtrar origem por `telegram_session_key`.

Bot publicador é obrigatório quando existe ao menos um botão inline ativo.

## 5. Debug temporário

Durante esta correção, registre no backend (não no frontend e sem segredos):
- telegram_session_key recebido no heartbeat;
- número de chats sincronizados por session key;
- keys de publisher_bots recebidas;
- quantidade de contas ativas retornadas ao seletor;
- quantidade de bots ativos retornados ao seletor.

Nunca logar token.

## 6. Critérios de aceite

Só concluir quando:
1. heartbeat de `primary` mantém/atualiza Conta Principal;
2. heartbeat de `marca_b` cria/atualiza Conta Marca B;
3. seletor `Conta Telegram` mostra as duas;
4. selecionar `marca_b` mostra os chats sincronizados por `marca_b`;
5. heartbeat com publisher_bots cria/atualiza os bots lógicos;
6. seletor Bot publicador mostra os bots conectados/ativos;
7. automação salva `telegram_session_key` e `telegram_bot_key` corretos;
8. botão permite Padrão/Azul/Verde/Vermelho;
9. API do worker devolve `style` corretamente;
10. replacements, blacklist, mídia, caption, formatting, logs e message links continuam intactos;
11. nenhuma credencial sensível é armazenada ou exibida.

Inspecione o código atual antes de alterar e faça correção cirúrgica. Não recrie telas/tabelas se elas já existem; ajuste as estruturas atuais.