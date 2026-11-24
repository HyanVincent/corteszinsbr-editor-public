# Corteszinsbr — editor de cortes (versão pública)

Editor de clipes públicos dos cinco canais Kick autorizados, com enquadramento vertical, legendas automáticas e verificação de qualidade.

Este repositório foi criado **do zero** para evitar expor o histórico do projeto privado. Não contém tokens, configuração do Google Drive, arquivos de contas ou histórico operacional.

## Testar a reedição do Allan

O workflow **Editar clipe aprovado** é executado inicialmente por uma solicitação explícita registrada em `requests/allan-rerender.json`. Ele processa o clipe público indicado, roda verificações e deixa o MP4 como *artifact* para revisão manual na aba Actions. Não publica em redes sociais e não usa segredos.

### Segurança
- Não adicionar tokens, cookies, arquivos `.env` ou configuração `rclone` ao repositório.
- Artifacts de vídeo em repositório público são publicamente acessíveis.
- O envio privado ao Drive ficará para uma segunda etapa, com uma integração de segurança separada.
- Não reutilizar o histórico Git do repositório privado.
