# Prompt para o Lovable — Botões inline + múltiplos bots publicadores

Quero evoluir a tela atual de criação/edição de automações para suportar botões inline do Telegram e seleção de um bot publicador por automação. Não quebre nenhuma funcionalidade existente.

## Contexto

O worker externo continua usando a sessão Telegram atual para LER as fontes, aplicar blacklist/replacements, recovery e demais regras. Para mensagens com botão inline, a publicação pode ser feita por um bot Telegram.

O worker agora suporta VÁRIOS bots configurados privadamente na AWS. O Lovable NÃO deve armazenar nem receber os tokens dos bots. O painel deve trabalhar apenas com uma chave pública estável do bot, por exemplo `north`, `marca_b`, `grupo_vip`.

Tokens são segredo de infraestrutura e ficam somente no ambiente da AWS em `TELEGRAM_BOTS_JSON`.

## 1. Cadastro lógico de bots no Lovable

Criar uma área simples chamada **Bots publicadores**.

Cada bot cadastrado no Lovable deve guardar apenas:
- `id`
- `key` — slug/chave única e imutável após uso, ex.: `north`, `marca_b`
- `name` — nome amigável, ex.: `North Finance Bot`
- `telegram_username` — opcional, ex.: `@northfinance_bot`
- `enabled` — boolean
- timestamps conforme padrão do projeto

NUNCA criar coluna para token. NUNCA pedir token no frontend. NUNCA devolver token em API.

Sugestão de tabela: `telegram_publisher_bots`.

Validações:
- `key` obrigatória, única, lowercase e aceita apenas letras, números, `_`, `-` e `.`;
- `name` obrigatório;
- impedir exclusão de bot que esteja em uso por uma automação sem antes exigir troca/remover vínculo;
- permitir ativar/desativar.

UI exemplo:

Bots publicadores
- North Finance Bot — chave `north` — ativo
- Marca B Bot — chave `marca_b` — ativo
[+ Adicionar bot]

Ao adicionar um bot, mostrar aviso:
`A chave deste bot deve existir com o mesmo valor em TELEGRAM_BOTS_JSON no worker da AWS. O token fica somente na AWS e nunca é salvo no Lovable.`

## 2. Seleção do bot dentro da automação

Na MESMA tela de criar/editar automação, adicionar um campo:

**Bot publicador**
[ selecionar bot ▼ ]

Listar somente bots ativos da tabela `telegram_publisher_bots`.

Persistir na automação o campo `telegram_bot_key` usando a `key` pública do bot, NÃO o token.

Exemplo:
```json
"telegram_bot_key": "north"
```

Quando houver mais de um bot ativo e a automação tiver botões habilitados, tornar a seleção do bot obrigatória.

Ao editar uma automação, carregar o bot já selecionado.
Ao duplicar/clonar, duplicar também `telegram_bot_key`.

## 3. Botões inline

Na MESMA tela/formulário, criar seção **Botões da mensagem**.

- Toggle `Adicionar botão à mensagem`;
- um ou mais botões;
- `Texto do botão` obrigatório;
- `URL` obrigatória com `http://`, `https://` ou `tg://`;
- `Linha` / `row`;
- `Ordem` / `sort_order`;
- `Ativo`;
- botão `+ Adicionar outro botão`;
- remover botão;
- prévia simples opcional.

Se usar tabela relacionada, sugestão `automation_buttons`:
- id
- automation_id
- text
- url
- row_index
- sort_order
- enabled
- timestamps

Siga o mesmo padrão arquitetural já usado por replacements/blacklist. Se o projeto usa JSON/JSONB para configurações compostas, pode seguir esse padrão. Não faça migração destrutiva.

## 4. Endpoint existente do worker

Não renomear nem alterar autenticação de:
`GET /api/public/worker/automations`

Continuar usando `x-worker-secret`.

Cada automação deve incluir, além de TODOS os campos atuais:

```json
{
  "id": "automation-id",
  "source_chat_id": "-100111",
  "destination_chat_id": "-100222",
  "telegram_bot_key": "north",
  "replacements": [],
  "blacklist": [],
  "buttons": [
    {
      "id": "button-id",
      "text": "🔥 ACESSAR AGORA",
      "url": "https://site.com/oferta",
      "row": 0,
      "sort_order": 0,
      "enabled": true
    }
  ]
}
```

Para automações sem botões:
```json
"buttons": []
```

Aceite `row_index` internamente, mas normalize o payload do worker para `row`.
Ordenar por `row` e depois `sort_order`.

## 5. Compatibilidade

Não alterar:
- source/destination;
- replacements;
- blacklist;
- preserve_media;
- preserve_caption;
- preserve_formatting;
- message links;
- autenticação do worker;
- demais fluxos existentes.

Automações antigas sem botões precisam continuar funcionando normalmente.

## 6. Avisos na UI

Mostrar próximo ao seletor de bot:
`O bot selecionado deve estar cadastrado na AWS com a mesma chave e ser administrador do canal de destino com permissão para publicar mensagens.`

Mostrar na área de botões:
`Botões inline são aplicados a mensagens únicas (texto, foto, vídeo ou arquivo). Álbuns/grupos de mídia do Telegram não suportam teclado inline diretamente no próprio álbum.`

## 7. Resultado esperado

Depois da alteração eu devo conseguir:
1. cadastrar no Lovable `North Finance Bot` com chave `north`;
2. cadastrar `Marca B Bot` com chave `marca_b`;
3. abrir Nova Automação;
4. escolher fonte/destino normalmente;
5. escolher `North Finance Bot` no campo Bot publicador;
6. configurar replacements/blacklist normalmente;
7. habilitar botão inline e informar texto/URL;
8. salvar;
9. o endpoint do worker devolver `telegram_bot_key: "north"` e o array `buttons`;
10. o worker AWS usar o token privado correspondente à chave `north`;
11. outra automação poder escolher `marca_b` sem afetar a primeira.

Implemente frontend, backend, tipos/interfaces, persistência, migrações e validações necessárias, reutilizando os padrões visuais e arquiteturais atuais. Não faça refatoração ampla fora do escopo.