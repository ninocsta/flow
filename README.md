# Flow

**Um painel para quem roda várias sessões do [Claude Code](https://claude.com/claude-code) ao mesmo tempo, com uma agenda que não dorme.**

Com 8 ou 9 sessões abertas, o gargalo deixa de ser o Claude: passa a ser a sua atenção. O Flow mostra tudo numa barra só, diz quem está esperando por você, troca de sessão sozinho quando você responde e, quando você pede para ser lembrado de alguma coisa, ele lembra, mesmo que a sessão que pediu já tenha sido fechada.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✂️  Cortar o cabelo · 18:20      Começa às 18:20   [+10 min]  [ OK ]  │  ← pisca e toca até você dar OK
├──────────────────────────────────────────────────────────────────────┤
│ 💼 3  🛠    ⏰ 2   🕘 recentes   ⏸ 1   Auto   ◧ lado   +              │
├──────────────────────────────────────────────────────────────────────┤
│ ⏰ AGENDA · 2                                          + agendar      │
│  🏦 Conferir o débito automático     qua. 08:07   ▶  🕑  ×            │
│  📅 Daily · em 10 min                qua. 09:50   ▶  🕑  ×            │
│ ATIVAS · 3                                                            │
│  ● Ajuste no checkout            Pede confirmação: Bash        agora  │
│  ● Revisão do PR 42              Pronto, deixei dois comentários  5m  │
│  ● Migração de dados             trabalhando…                         │
└──────────────────────────────────────────────────────────────────────┘
```

## O que ele faz

### Sessões
- **Tudo numa barra:** cada sessão viva vira um chip com título, último resumo, tempo esperando você, % de contexto e custo equivalente na API.
- **Quem pede você primeiro:** quem pede confirmação vem antes, depois quem terminou e por último quem ainda trabalha. Também avisa quando uma sessão termina ou trava pedindo permissão.
- **Auto:** quando você responde a sessão aberta, ele vai sozinho para a próxima que espera você. Não troca enquanto você digita, enquanto o microfone grava (ditado) ou se a sessão estiver fixada 📌.
- **Em espera:** tira uma sessão da frente com uma nota ("esperando o PR do fulano") e um lembrete opcional.
- **Modo serviço:** a sessão fica quieta em espera e só aparece quando chama você (`flow-chamar "motivo"`).
- **Separa trabalho de pessoal** pela pasta da sessão e permite **retomar sessões recentes** com um clique.

### Agenda
Três tipos de item, criados pelo painel ou pela linha de comando:

| Tipo | O que acontece na hora |
|---|---|
| ⏰ **aviso** | Faixa piscando no topo do Flow, som de alarme repetido a cada 2 min e notificação. Fica assim até você dar **OK** ou **+10 min**. |
| ↻ **retomar** | Volta a uma sessão pelo id e digita o prompt. Se ela estiver aberta, espera ficar livre. Se estiver fechada, reabre com `claude --resume`. |
| ✨ **nova** | Abre uma sessão nova numa pasta, já com o prompt. |

- **Não perde nada:** o que venceu com o Flow fechado ou o PC suspenso roda quando ele voltar (até 12 h de atraso). Passou disso, fica em **⚠ decidir** com as opções *rodar* ou *descartar*.
- **Recorrência:** `diario 07:50`, `seg-sex 08:07`, `seg,qua 10:00`.
- **Ícone por assunto:** você escolhe o emoji ou ele adivinha pelo texto (✂️ cabelo, 📅 reunião, 🩺 médico, 🚀 deploy, 💸 boleto…).
- **Reuniões (opcional):** a cada 5 min o Flow consulta uma API de agenda e cria avisos 10 min e 1 min antes de cada reunião. Clicar no aviso entra no Meet. Eventos recusados ou cancelados somem sozinhos.

## Agendar de qualquer IA

O `flow-agendar` é um comando comum: qualquer agente que tenha terminal (Claude Code, Codex, Gemini CLI…) consegue usar. Basta uma linha no `CLAUDE.md` / `AGENTS.md` dizendo "lembretes → `flow-agendar`".

```sh
# retoma ESTA sessão amanhã e digita o prompt (a sessão é achada pelo pid)
flow-agendar "amanhã 08:07" "Confere se a cobrança de hoje passou e me resume"

# só um lembrete
flow-agendar --aviso "amanhã 17:40" "Cortar o cabelo às 18:20" --icone ✂️

# sessão nova todo dia útil
flow-agendar --nova --cwd ~/projetos --recorrente "seg-sex 08:00" "/dia"

flow-agendar --lista
flow-agendar --cancelar a1b2c3d4
```

Formatos de hora: `08:07`, `amanhã 8h`, `18/11 09:00`, `2026-11-18 08:07`, `+30m`, `+2h`.

## Como funciona

```
 ~/.claude*/sessions/*.json ─┐                         ┌─► kitty @ send-text (digita na sessão)
 transcripts .jsonl ─────────┼─► server.py (stdlib) ───┼─► claude --resume <id> "prompt"
 agenda.json ────────────────┘    porta 8765           ├─► hyprctl (foca/organiza janelas)
                                  watcher a cada 1 s   ├─► notify-send + paplay (alarme)
 flow-agendar / flow-chamar ──HTTP──►                  └─► index.html (janela Chrome --app)
```

- **Zero dependências:** Python 3 só com a biblioteca padrão e uma página HTML.
- **Estado em `~/.local/share/flow/`:** `tags.json` (títulos, notas, espera), `agenda.json` e `config.json`.
- **Regras pessoais em `~/.local/share/flow/regras.json`** (opcional, fica fora do repositório): prefixos de código de tarefa (`DEM-12`), skill → assunto e palavra → assunto, usados para agrupar as sessões.
- **Só aceita conexões locais:** escuta apenas em `127.0.0.1`, e os POSTs exigem o header `X-Painel: 1`.

## Requisitos

Feito para **Linux + Hyprland (Wayland) + kitty**, que é o setup do autor:

- Python 3.10+, Claude Code (`claude`), kitty com `allow_remote_control` (para digitar nas sessões)
- `hyprctl`, `notify-send` (testado com mako), `paplay`, `jq`, `curl`, Google Chrome (janela `--app`)
- Opcional: `hypridle` criando `$XDG_RUNTIME_DIR/painel-ocioso` após alguns segundos sem uso (deixa o Auto mais esperto)

Em outro ambiente, a parte de janelas (`hyprctl`) e de terminal (`kitty @`) precisa ser adaptada. A agenda e a leitura das sessões são independentes disso.

## Instalação

```sh
git clone https://github.com/ninocsta/flow ~/github/flow
ln -s ~/github/flow/flow    ~/.local/bin/flow
ln -s ~/github/flow/agendar ~/.local/bin/flow-agendar
ln -s ~/github/flow/chamar  ~/.local/bin/flow-chamar
flow   # sobe o servidor (se preciso) e abre a janela
```

Para subir junto com o Hyprland: `exec-once = sleep 6; ~/.local/bin/flow`.

## Arquivos

| Arquivo | |
|---|---|
| `server.py` | servidor HTTP, watcher das sessões, agenda, integração com Hyprland/kitty |
| `index.html` | a interface (barra, lista, agenda, diálogos) |
| `flow` | abre a janela e sobe o servidor se ele não estiver no ar |
| `agendar` | CLI `flow-agendar` |
| `chamar` | CLI `flow-chamar` (modo serviço) |

---

Projeto pessoal, feito em conversa com o próprio Claude Code. Use, adapte e quebre à vontade.
