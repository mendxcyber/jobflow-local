# Segurança e privacidade

Protótipo para uso local por uma pessoa. Não exponha o serviço à internet.
Não há autenticação de produção nem garantia de compatibilidade com todos os sites.
As portas do Compose ficam em loopback. A validação de origem/host reduz escritas
vindas de páginas externas, mas não substitui autenticação nem protege contra
outros processos locais. Não use como serviço compartilhado.

Credenciais SMTP podem ficar em dados/smtp-private.json ou no ambiente. O volume
dados e o .env devem permanecer privados, inclusive nos backups. Não copie esses
arquivos para issues, screenshots, repositórios ou pacotes de demonstração.

A extensão usa permissões amplas e pode transmitir dados ao painel configurado e
ao portal de candidatura. Ela inicia desligada na edição pública. O modelo local
recebe somente perguntas e fatos revisados; não deve receber senhas nem testes.

Confirmação por texto é heurística. Aceitação SMTP não garante entrega, leitura
ou legitimidade do destinatário. Confira o endereço antes de enviar e investigue
envios incertos antes de repetir. Tokens de tentativa e hashes não equivalem a
uma auditoria independente nem garantem ausência de erros.
Relate falhas sem publicar credenciais ou dados pessoais. Use os canais privados
de reporte do GitHub quando estiverem disponíveis.
