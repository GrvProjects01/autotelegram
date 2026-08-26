# Prompt para o Lovable — Botões inline por automação

Quero adicionar suporte a botões inline do Telegram DENTRO da tela atual de criação/edição de automações. Não crie uma página ou módulo separado de botões.

## Contexto do sistema atual

A automação já possui seleção de canal/grupo fonte, canal/grupo destino, replacements, blacklist e opções de preservação de mídia/caption/formatação. O worker externo consulta as automações pelo endpoint existente:

`GET /api/public/worker/automations`

Não altere nem quebre nenhuma funcionalidade existente. Esta mudança deve ser retrocompatível: automações sem botão devem continuar retornando e funcionando exatamente como hoje.

## Objetivo

Na MESMA tela/formulário de criar e editar automação, abaixo das configurações existentes (preferencialmente depois de Replacements/Blacklist), criar uma seção chamada **Botões da mensagem**.

A seção deve ter:

- Toggle: `Adicionar botão à mensagem`.
- Quando desligado, nenhum botão é salvo/aplicado.
- Quando ligado, permitir cadastrar um ou mais botões.
- Cada botão deve ter:
  - `Texto do botão` (obrigatório, máximo razoável de UI, ex. 64 caracteres).
  - `URL` (obrigatória; aceitar apenas URLs válidas `http://`, `https://` ou `tg://`).
  - `Linha` / organização visual. Por padrão, cada botão deve ficar em uma linha. Opcionalmente permitir posicionar dois ou mais na mesma linha.
  - `Ordem`.
  - `Ativo`.
- Botão `+ Adicionar outro botão`.
- Possibilidade de remover um botão antes de salvar.
- Mostrar uma prévia simples do teclado inline.

Exemplo visual:

Botões da mensagem
[✓] Adicionar botão

Botão 1
Texto: [🔥 ACESSAR AGORA]
URL:   [https://site.com/oferta]
Linha: [1]

[+ Adicionar outro botão]

## Persistência

Primeiro inspecione a modelagem atual das automações, replacements e blacklist e siga o MESMO padrão arquitetural do projeto.

Preferência: se replacements/blacklist usam tabelas relacionadas, criar uma tabela relacionada `automation_buttons`. Se o projeto já armazena configurações compostas em JSON/JSONB, pode seguir o padrão existente. NÃO faça migração destrutiva.

Se usar tabela relacionada, estrutura sugerida:

- `id`
- `automation_id` — FK para a automação
- `text`
- `url`
- `row_index` — inteiro, default 0
- `sort_order` — inteiro, default 0
- `enabled` — boolean, default true
- timestamps conforme padrão do projeto

Garanta cascade/delete consistente com as outras relações da automação e RLS/policies coerentes com o projeto existente.

## CRUD da automação

Estenda o fluxo ATUAL de criar/editar automação para salvar os botões no mesmo submit da tarefa.

Ao editar uma automação:
- carregar os botões já cadastrados;
- permitir adicionar, alterar, ordenar, ativar/desativar e remover;
- não afetar source, destination, replacements, blacklist nem demais campos.

Ao duplicar/clonar uma automação, caso o sistema já possua essa ação, duplicar também os botões.

## Endpoint do worker

O endpoint existente `GET /api/public/worker/automations` precisa passar a incluir um array `buttons` em CADA automação, sem remover nenhum campo atual.

Formato obrigatório para o worker:

```json
{
  "id": "automation-id",
  "source_chat_id": "-100111",
  "destination_chat_id": "-100222",
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

Para automações sem botões, retornar obrigatoriamente:

```json
"buttons": []
```

Aceite internamente `row_index`, mas normalize a resposta do worker para a propriedade `row`.

Ordenar os botões por `row` e depois por `sort_order`.

## Validações

- Não permitir botão sem texto.
- Não permitir botão sem URL.
- Validar protocolo da URL (`http`, `https`, `tg`).
- Não salvar linhas vazias.
- Sanitizar/trimar texto e URL.
- Um erro nos botões não pode apagar replacements ou outras configurações da automação.

## Aviso de compatibilidade com Telegram

Exibir um aviso pequeno na seção:

`Botões inline são aplicados a mensagens únicas (texto, foto, vídeo ou arquivo). Álbuns/grupos de mídia do Telegram não suportam teclado inline no próprio álbum.`

Também exibir:

`Para publicar botões, o bot configurado no worker precisa ser administrador do canal de destino com permissão para publicar mensagens.`

## Backend/API

Atualize todos os tipos/interfaces/schemas/serializers necessários para que `buttons` faça parte da automação sem quebrar clientes antigos.

Não renomeie endpoints existentes.
Não altere autenticação do worker (`x-worker-secret`).
Não remova campos existentes.
Não altere a lógica de replacements/blacklist.
Não altere a forma de seleção de source/destination.

## Resultado esperado

Depois desta alteração eu devo conseguir:

1. Abrir `Nova Automação`.
2. Selecionar fonte e destino como já faço hoje.
3. Cadastrar replacements/blacklist como já faço hoje.
4. Na mesma página ativar `Adicionar botão à mensagem`.
5. Informar texto e URL.
6. Salvar a automação uma única vez.
7. Editar depois e ver os mesmos botões cadastrados.
8. O endpoint `/api/public/worker/automations` devolver esses botões dentro da automação.
9. Automações antigas sem botão continuarem funcionando normalmente.

Implemente a funcionalidade completa no frontend e backend do projeto Lovable, incluindo persistência/migração quando necessária. Antes de alterar, inspecione o código existente e reutilize os componentes, padrões visuais e padrões de dados já adotados pelo projeto. Não recrie telas desnecessariamente e não faça refatoração ampla fora do escopo.