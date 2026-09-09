# Prompt para o Lovable — múltiplas contas/sessões Telegram por automação

Quero evoluir o projeto atual para suportar várias contas/sessões Telegram de leitura no worker AWS. Cada automação deve escolher QUAL conta Telegram será responsável por ler o canal/grupo de origem daquela tarefa.

Não quebre source/destination, replacements, blacklist, botões inline, bots publicadores, logs, message-links, recovery ou autenticação do worker.

## Conceito

O worker AWS agora pode executar várias sessões de usuário Telegram em processos isolados. Cada sessão possui uma chave pública estável, por exemplo:

- `primary` — Conta principal
- `marca_b` — Conta da Marca B
- `conta_ofertas` — Conta Ofertas

O Lovable NÃO deve receber arquivo `.session`, telefone, código de login, senha 2FA ou qualquer segredo da conta Telegram. O painel trabalha somente com uma chave/nome lógico.

## 1. Cadastro lógico de contas Telegram

Criar uma área **Contas Telegram de leitura**.

Cada registro deve conter:
- `id`
- `key` — chave única, lowercase, ex. `primary`, `marca_b`
- `name` — nome amigável, ex. `Conta Principal`, `Conta Marca B`
- `telegram_user_id` — opcional
- `telegram_username` — opcional
- `enabled` — boolean
- timestamps conforme padrão do projeto

Sugestão de tabela: `telegram_reader_accounts`.

Nunca criar colunas para:
- telefone;
- código Telegram;
- senha 2FA;
- API hash;
- arquivo/session string.

Mostrar aviso:
`A sessão real desta conta é criada e armazenada somente no worker AWS. A chave cadastrada aqui deve ser igual a TELEGRAM_SESSION_KEY do serviço correspondente.`

## 2. Seleção dentro da automação

Na mesma tela de Nova Automação / Editar Automação adicionar campo obrigatório:

**Conta Telegram de origem**
[ selecionar conta ▼ ]

Persistir na automação:

```json
"telegram_session_key": "marca_b"
```

Listar apenas contas ativas.

Ao editar, carregar a conta selecionada.
Ao duplicar/clonar, duplicar `telegram_session_key`.

Para automações antigas sem esse campo, considerar a conta `primary` como fallback durante a migração, sem alterar os demais dados.

## 3. Filtrar canais disponíveis pela conta

Se o projeto já relaciona chats sincronizados a uma `telegram_account_id`, mantenha e aproveite essa relação.

Quando o usuário selecionar `Conta Telegram de origem`, o seletor de **Canal/Grupo de origem** deve preferencialmente mostrar apenas chats sincronizados por aquela conta.

Objetivo:
- `primary` enxerga seus chats;
- `marca_b` enxerga os chats da segunda conta;
- um canal privado que só existe em `marca_b` aparece quando essa conta é escolhida.

Não misturar silenciosamente chats de contas diferentes.

## 4. Endpoint do worker

Manter:
`GET /api/public/worker/automations`

e autenticação atual `x-worker-secret`.

Cada automação deve incluir:

```json
{
  "id": "automation-id",
  "telegram_session_key": "marca_b",
  "source_chat_id": "-100111",
  "destination_chat_id": "-100222",
  "telegram_bot_key": "north",
  "replacements": [],
  "blacklist": [],
  "buttons": []
}
```

Não remover nenhum campo existente.

## 5. Heartbeat / sincronização

O sistema já recebe heartbeat e sincronização de chats do worker. Preserve esse fluxo.

Se necessário para diferenciar duas instâncias do mesmo código, aceite/registre `worker_id` distintos e associe os chats ao `telegram_user_id` retornado por cada sessão.

Quando houver metadados suficientes, permita que a área `Contas Telegram de leitura` mostre status:
- Online
- Offline
- username/id sincronizado

Nunca exponha segredos.

## 6. Resultado esperado

Depois da alteração eu devo conseguir:
1. cadastrar `Conta Principal` com chave `primary`;
2. cadastrar `Conta Marca B` com chave `marca_b`;
3. criar uma automação;
4. escolher `Conta Marca B`;
5. visualizar/selecionar um canal privado sincronizado por essa conta;
6. salvar a automação;
7. `/api/public/worker/automations` retornar `telegram_session_key: "marca_b"`;
8. somente o processo AWS da sessão `marca_b` processar essa tarefa;
9. outra automação usar `primary` simultaneamente;
10. ambas funcionarem 24/7 sem depender do computador local.

Implemente frontend, backend, persistência, tipos/interfaces e validações necessárias reutilizando os padrões atuais do projeto. Não faça refatoração ampla fora do escopo.
