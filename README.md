# 👥 CRM

Sistema de gestão de clientes e vendas com importação/exportação de planilhas.

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python + Flask |
| Banco de dados | SQLite (`crm.db`) |
| Templates | Jinja2 |
| Processamento | Pandas + OpenPyXL |

## Funcionalidades

- Cadastro e gestão de clientes (código automático `CLI-XXXX`)
- Registro de vendas (código automático `VND-XXXX`)
- Upload e importação de planilhas Excel
- Exportação de relatórios
- Suporte a contatos: telefone, WhatsApp, Instagram, Facebook

## Instalação

```bash
cd /root/crm
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Rodando em Produção

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```

## Backup do Banco

```bash
scp minha-vps:/root/crm/crm.db ./crm_backup_$(date +%Y%m%d).db
```
