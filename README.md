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


## Descoberta automática

O workflow `Corteszinsbr - descobrir e editar (publico)` consulta a Kick a cada **4 horas** (agendamento GitHub pode sofrer atrasos), examina os cinco canais autorizados e enfileira **no máximo 6 edições por dia**. A lista de IDs já despachados é armazenada em `data/hourly_seen.json`, para evitar duplicações. O primeiro teste do novo fluxo foi concluído com sucesso. Nenhuma etapa publica nas redes sociais.

> Como este repositório é público, arquivos e logs de execução, incluindo os MP4 nos artifacts, também podem ser acessados por outras pessoas. Os artifacts ficam disponíveis por 7 dias.

## Google Drive — entrega privada opcional

Esta integração está **desativada por padrão**. Para habilitar, o proprietário deve abrir **Settings → Secrets and variables → Actions → New repository secret** e inserir **somente como segredos do GitHub**, nunca em arquivos, comentários, issues ou mensagens:

- `RCLONE_CONFIG_B64`: conteúdo Base64 da configuração rclone autorizada exclusivamente para a pasta de revisão; contém credenciais sensíveis e não deve ser publicado
- `DRIVE_REVIEW_FOLDER_ID`: ID da pasta de revisão do Drive de destino

Uma vez presentes ambos os segredos, cada edição concluída tentará enviar os MP4 e metadados de QA para `revisao-publica-<run_number>`. Cortes que exigem revisão editorial são enviados ao subdiretório `pendente`. O editor jamais publica no TikTok ou YouTube.

**Não copie a configuração rclone de um arquivo para o repositório público**; use exclusivamente o formulário de segredos. Recomenda-se um acesso Google de privilégio mínimo, revisar permissões de contribuidores e desativar fluxos originados de pull requests de terceiros com acesso a segredos. O repositório não executa workflows com segredos em pull requests.

Antes de editar ou inserir credenciais, revise as permissões do projeto e os custos eventuais dos serviços usados (o GitHub Actions em repositórios públicos usa a política de gratuidade vigente para runners padrão).

