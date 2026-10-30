# Limpa Mac

Painel local (Python puro, sem dependências) para ver o que ocupa memória e disco no Mac e mandar para a Lixeira com um clique.

```bash
python3 app.py      # abre http://127.0.0.1:8765
```

## O que mostra
- Disco usado, quanto dá para liberar e quanto disso é seguro.
- Memória livre, swap e os processos que mais usam memória.
- Lista do que pode sair, com o motivo e a etiqueta **Seguro** ou **Confirmar**:
  cópias de repositório (git worktrees, conferidas uma a uma: só "Seguro" se não há alteração nem commit fora do remoto),
  Downloads grandes, caches (npm, Chrome, CocoaPods, Playwright, Electron, Expo), builds (`dist`, `.next`, `.open-next`,
  DerivedData do Xcode), `node_modules` dos projetos, Docker e simuladores do iPhone.

## Segurança
- Nada é apagado direto: "Mover para a Lixeira" faz `mv` para `~/.Trash`; esvaziar a Lixeira é um botão separado, com confirmação.
- Itens "Confirmar" pedem confirmação; worktree com commit fora do remoto é bloqueado.
- Pastas de credenciais e chaves nunca entram na lista.
- Cada ação fica em `historico.jsonl` (local, fora do git).
- Escuta só em 127.0.0.1.
