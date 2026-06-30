from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
import sqlite3
import json
import os
import io
from datetime import datetime
import pandas as pd
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'crm_secret_2024_change_me'
DATABASE = 'crm.db'
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def next_client_code(conn):
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(client_code, 5) AS INTEGER)) FROM clients WHERE client_code LIKE 'CLI-%'"
    ).fetchone()
    return f'CLI-{(row[0] or 0) + 1:04d}'


def next_sale_code(conn):
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(sale_code, 5) AS INTEGER)) FROM purchases WHERE sale_code LIKE 'VND-%'"
    ).fetchone()
    return f'VND-{(row[0] or 0) + 1:04d}'


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            whatsapp TEXT,
            instagram TEXT,
            facebook TEXT,
            type TEXT DEFAULT 'potential',
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_contact DATE
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            value REAL DEFAULT 0,
            purchase_date DATE NOT NULL,
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
        CREATE TABLE IF NOT EXISTS purchase_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0,
            subtotal REAL DEFAULT 0,
            FOREIGN KEY (purchase_id) REFERENCES purchases(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
        CREATE TABLE IF NOT EXISTS contact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            template_name TEXT,
            message TEXT,
            contact_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );
    ''')
    conn.commit()
    conn.close()

    conn = get_db()

    # Legacy migrations
    for col, typedef in [('sku', 'TEXT'), ('description', 'TEXT')]:
        try:
            conn.execute(f'ALTER TABLE products ADD COLUMN {col} {typedef}')
            conn.commit()
        except Exception:
            pass

    # Melhoria 2: client_code and sale_code columns
    for table, col_def in [('clients', 'client_code TEXT'), ('purchases', 'sale_code TEXT')]:
        try:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col_def}')
            conn.commit()
        except Exception:
            pass

    # Generate client_code for existing clients without one
    max_row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(client_code, 5) AS INTEGER)) FROM clients WHERE client_code LIKE 'CLI-%'"
    ).fetchone()
    max_n = max_row[0] or 0
    for i, c in enumerate(
        conn.execute('SELECT id FROM clients WHERE client_code IS NULL ORDER BY id').fetchall(),
        start=max_n + 1
    ):
        conn.execute("UPDATE clients SET client_code = ? WHERE id = ?", (f'CLI-{i:04d}', c['id']))

    # Generate sale_code for existing purchases without one
    max_row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(sale_code, 5) AS INTEGER)) FROM purchases WHERE sale_code LIKE 'VND-%'"
    ).fetchone()
    max_n = max_row[0] or 0
    for i, p in enumerate(
        conn.execute('SELECT id FROM purchases WHERE sale_code IS NULL ORDER BY id').fetchall(),
        start=max_n + 1
    ):
        conn.execute("UPDATE purchases SET sale_code = ? WHERE id = ?", (f'VND-{i:04d}', p['id']))

    conn.commit()

    # Unique index on products.sku (partial: only non-null, non-empty values)
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku ON products(sku) "
            "WHERE sku IS NOT NULL AND sku != ''"
        )
        conn.commit()
    except Exception:
        pass

    # Migrate purchases → purchase_items for orphaned records
    orphans = conn.execute('''
        SELECT p.id, p.product_id, p.product_name, p.value
        FROM purchases p
        LEFT JOIN purchase_items pi ON pi.purchase_id = p.id
        WHERE pi.id IS NULL AND p.product_name != ''
    ''').fetchall()
    for pu in orphans:
        conn.execute(
            'INSERT INTO purchase_items (purchase_id, product_id, product_name, quantity, unit_price, subtotal) VALUES (?,?,?,?,?,?)',
            (pu['id'], pu['product_id'], pu['product_name'], 1, pu['value'], pu['value']))
    conn.commit()
    conn.close()


# ===== FILTROS JINJA2 =====

@app.template_filter('currency')
def currency_filter(value):
    try:
        v = float(value)
        return f'R$ {v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    except Exception:
        return 'R$ 0,00'


@app.template_filter('fmt_date')
def fmt_date(value):
    if not value:
        return '—'
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
    except Exception:
        return str(value)


@app.template_filter('fmt_datetime')
def fmt_datetime(value):
    if not value:
        return '—'
    try:
        return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')
    except Exception:
        return str(value)


@app.context_processor
def inject_globals():
    conn = get_db()
    potential_count = conn.execute("SELECT COUNT(*) FROM clients WHERE type='potential'").fetchone()[0]
    conn.close()
    return {'nav_potential_count': potential_count}


@app.template_filter('wa_url')
def wa_url_filter(phone):
    if not phone:
        return '#'
    digits = ''.join(c for c in str(phone) if c.isdigit())
    if len(digits) < 8:
        return '#'
    if not digits.startswith('55'):
        digits = '55' + digits
    return f'https://wa.me/{digits}'


@app.template_global('enumerate')
def jinja_enumerate(iterable, start=0):
    return enumerate(iterable, start=start)


# ===== DASHBOARD =====

@app.route('/')
def dashboard():
    conn = get_db()
    stats = {
        'total_clients': conn.execute('SELECT COUNT(*) FROM clients').fetchone()[0],
        'active_clients': conn.execute("SELECT COUNT(*) FROM clients WHERE type='active'").fetchone()[0],
        'potential_clients': conn.execute("SELECT COUNT(*) FROM clients WHERE type='potential'").fetchone()[0],
        'total_revenue': conn.execute('SELECT COALESCE(SUM(value),0) FROM purchases').fetchone()[0],
        'total_purchases': conn.execute('SELECT COUNT(*) FROM purchases').fetchone()[0],
    }
    top_by_value = conn.execute('''
        SELECT c.id, c.name, c.type, c.whatsapp, c.phone, c.client_code,
               COUNT(p.id) AS buy_count,
               COALESCE(SUM(p.value), 0) AS total_spent
        FROM clients c JOIN purchases p ON c.id = p.client_id
        GROUP BY c.id ORDER BY total_spent DESC LIMIT 10
    ''').fetchall()
    top_by_freq = conn.execute('''
        SELECT c.id, c.name, c.type, c.client_code,
               COUNT(p.id) AS buy_count,
               COALESCE(SUM(p.value), 0) AS total_spent
        FROM clients c JOIN purchases p ON c.id = p.client_id
        GROUP BY c.id ORDER BY buy_count DESC LIMIT 10
    ''').fetchall()
    top_products = conn.execute('''
        SELECT pi.product_name, COUNT(*) AS sale_count, COALESCE(SUM(pi.subtotal), 0) AS revenue
        FROM purchase_items pi GROUP BY pi.product_name ORDER BY sale_count DESC LIMIT 10
    ''').fetchall()
    potential = conn.execute('''
        SELECT id, name, phone, whatsapp, instagram, created_at, last_contact
        FROM clients WHERE type = 'potential'
        ORDER BY CASE WHEN last_contact IS NULL THEN 0 ELSE 1 END,
                 last_contact ASC, created_at DESC LIMIT 15
    ''').fetchall()
    recent_purchases = conn.execute('''
        SELECT pu.id, pu.sale_code, c.id AS client_id, c.name AS client_name, c.client_code,
               pu.product_name, pu.value, pu.purchase_date
        FROM purchases pu JOIN clients c ON pu.client_id = c.id
        ORDER BY pu.created_at DESC LIMIT 10
    ''').fetchall()
    conn.close()
    return render_template('dashboard.html',
        stats=stats, top_by_value=top_by_value, top_by_freq=top_by_freq,
        top_products=top_products, potential=potential, recent_purchases=recent_purchases)


# ===== CLIENTES =====

@app.route('/clients')
def clients_list():
    search = request.args.get('search', '').strip()
    ctype = request.args.get('type', '')
    conn = get_db()
    q = '''
        SELECT c.id, c.client_code, c.name, c.phone, c.whatsapp, c.instagram, c.type,
               c.last_contact, c.created_at,
               COUNT(p.id) AS buy_count,
               COALESCE(SUM(p.value), 0) AS total_spent
        FROM clients c LEFT JOIN purchases p ON c.id = p.client_id WHERE 1=1
    '''
    params = []
    if search:
        q += ' AND (c.name LIKE ? OR c.phone LIKE ? OR c.whatsapp LIKE ? OR c.instagram LIKE ? OR c.client_code LIKE ?)'
        params += [f'%{search}%'] * 5
    if ctype in ('active', 'potential'):
        q += ' AND c.type = ?'
        params.append(ctype)
    q += ' GROUP BY c.id ORDER BY c.name'
    clients = conn.execute(q, params).fetchall()
    conn.close()
    return render_template('clients/list.html', clients=clients, search=search, ctype=ctype)


@app.route('/clients/new', methods=['GET', 'POST'])
def client_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Nome é obrigatório.', 'danger')
            return render_template('clients/form.html', client=None)
        conn = get_db()
        code = next_client_code(conn)
        conn.execute(
            'INSERT INTO clients (client_code,name,phone,whatsapp,instagram,facebook,type,notes) VALUES (?,?,?,?,?,?,?,?)',
            (code, name, request.form.get('phone', '').strip(), request.form.get('whatsapp', '').strip(),
             request.form.get('instagram', '').strip(), request.form.get('facebook', '').strip(),
             request.form.get('type', 'potential'), request.form.get('notes', '').strip()))
        conn.commit()
        conn.close()
        flash(f'Cliente "{name}" cadastrado com sucesso! (ID: {code})', 'success')
        return redirect(url_for('clients_list'))
    return render_template('clients/form.html', client=None)


@app.route('/clients/<int:cid>')
def client_detail(cid):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone()
    if not client:
        flash('Cliente não encontrado.', 'danger')
        return redirect(url_for('clients_list'))
    purchases = conn.execute(
        'SELECT * FROM purchases WHERE client_id=? ORDER BY purchase_date DESC', (cid,)).fetchall()
    total_spent = conn.execute(
        'SELECT COALESCE(SUM(value),0) FROM purchases WHERE client_id=?', (cid,)).fetchone()[0]
    contacts = conn.execute(
        'SELECT * FROM contact_history WHERE client_id=? ORDER BY contact_date DESC LIMIT 20', (cid,)).fetchall()
    purchase_items_map = {}
    for pu in purchases:
        items = conn.execute('''
            SELECT pi.*, COALESCE(p.sku, '') AS product_sku
            FROM purchase_items pi
            LEFT JOIN products p ON p.id = pi.product_id
            WHERE pi.purchase_id = ? ORDER BY pi.id
        ''', (pu['id'],)).fetchall()
        purchase_items_map[pu['id']] = items
    conn.close()
    return render_template('clients/detail.html',
        client=client, purchases=purchases, total_spent=total_spent,
        contacts=contacts, purchase_items_map=purchase_items_map,
        today=datetime.now().strftime('%Y-%m-%d'))


@app.route('/clients/<int:cid>/edit', methods=['GET', 'POST'])
def client_edit(cid):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone()
    if not client:
        conn.close()
        flash('Cliente não encontrado.', 'danger')
        return redirect(url_for('clients_list'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Nome é obrigatório.', 'danger')
            conn.close()
            return render_template('clients/form.html', client=client)
        conn.execute(
            'UPDATE clients SET name=?,phone=?,whatsapp=?,instagram=?,facebook=?,type=?,notes=? WHERE id=?',
            (name, request.form.get('phone', '').strip(), request.form.get('whatsapp', '').strip(),
             request.form.get('instagram', '').strip(), request.form.get('facebook', '').strip(),
             request.form.get('type', 'potential'), request.form.get('notes', '').strip(), cid))
        conn.commit()
        conn.close()
        flash(f'Cliente "{name}" atualizado!', 'success')
        return redirect(url_for('client_detail', cid=cid))
    conn.close()
    return render_template('clients/form.html', client=client)


@app.route('/clients/<int:cid>/delete', methods=['POST'])
def client_delete(cid):
    conn = get_db()
    client = conn.execute('SELECT name FROM clients WHERE id=?', (cid,)).fetchone()
    if client:
        conn.execute('DELETE FROM purchases WHERE client_id=?', (cid,))
        conn.execute('DELETE FROM contact_history WHERE client_id=?', (cid,))
        conn.execute('DELETE FROM clients WHERE id=?', (cid,))
        conn.commit()
        flash(f'Cliente "{client["name"]}" removido.', 'success')
    conn.close()
    return redirect(url_for('clients_list'))


# ===== PRODUTOS =====

@app.route('/products')
def products_list():
    conn = get_db()
    products = conn.execute('''
        SELECT p.id, p.sku, p.name, p.description, p.price, p.created_at,
               COUNT(pi.id) AS sale_count,
               COALESCE(SUM(pi.subtotal), 0) AS revenue,
               COALESCE(AVG(pi.unit_price), 0) AS avg_value
        FROM products p LEFT JOIN purchase_items pi ON p.id = pi.product_id
        GROUP BY p.id ORDER BY p.name
    ''').fetchall()
    conn.close()
    return render_template('products/list.html', products=products)


@app.route('/products/new', methods=['GET', 'POST'])
def product_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Nome é obrigatório.', 'danger')
            return render_template('products/form.html', product=None)
        sku = request.form.get('sku', '').strip() or None
        try:
            price = float(request.form.get('price', 0) or 0)
        except ValueError:
            price = 0.0
        conn = get_db()
        if sku:
            existing = conn.execute('SELECT id FROM products WHERE sku = ?', (sku,)).fetchone()
            if existing:
                conn.close()
                flash(f'Já existe um produto com o SKU "{sku}". Escolha um SKU único.', 'danger')
                return render_template('products/form.html', product=None)
        conn.execute(
            'INSERT INTO products (sku, name, description, price) VALUES (?,?,?,?)',
            (sku, name, request.form.get('description', '').strip() or None, price))
        conn.commit()
        conn.close()
        flash(f'Produto "{name}" cadastrado!', 'success')
        return redirect(url_for('products_list'))
    return render_template('products/form.html', product=None)


@app.route('/products/<int:pid>/edit', methods=['GET', 'POST'])
def product_edit(pid):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not product:
        conn.close()
        flash('Produto não encontrado.', 'danger')
        return redirect(url_for('products_list'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Nome é obrigatório.', 'danger')
            conn.close()
            return render_template('products/form.html', product=product)
        sku = request.form.get('sku', '').strip() or None
        try:
            price = float(request.form.get('price', 0) or 0)
        except ValueError:
            price = 0.0
        if sku:
            existing = conn.execute('SELECT id FROM products WHERE sku = ? AND id != ?', (sku, pid)).fetchone()
            if existing:
                conn.close()
                flash(f'Já existe outro produto com o SKU "{sku}". Escolha um SKU único.', 'danger')
                return render_template('products/form.html', product=product)
        conn.execute(
            'UPDATE products SET sku=?,name=?,description=?,price=? WHERE id=?',
            (sku, name, request.form.get('description', '').strip() or None, price, pid))
        conn.commit()
        conn.close()
        flash(f'Produto "{name}" atualizado!', 'success')
        return redirect(url_for('products_list'))
    conn.close()
    return render_template('products/form.html', product=product)


@app.route('/products/<int:pid>/delete', methods=['POST'])
def product_delete(pid):
    conn = get_db()
    product = conn.execute('SELECT name FROM products WHERE id=?', (pid,)).fetchone()
    if product:
        conn.execute('DELETE FROM products WHERE id=?', (pid,))
        conn.commit()
        flash(f'Produto "{product["name"]}" removido.', 'success')
    conn.close()
    return redirect(url_for('products_list'))


@app.route('/api/products/<int:pid>')
def api_product(pid):
    conn = get_db()
    p = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    conn.close()
    if p:
        return jsonify({
            'id': p['id'], 'sku': p['sku'], 'name': p['name'],
            'description': p['description'], 'price': p['price']
        })
    return jsonify({'error': 'not found'}), 404


@app.route('/api/products/search')
def api_products_search():
    q = request.args.get('q', '').strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            'SELECT id, sku, name, price FROM products WHERE sku LIKE ? OR name LIKE ? ORDER BY name LIMIT 30',
            (f'%{q}%', f'%{q}%')).fetchall()
    else:
        rows = conn.execute(
            'SELECT id, sku, name, price FROM products ORDER BY name LIMIT 50').fetchall()
    conn.close()
    return jsonify([{'id': r['id'], 'sku': r['sku'] or '', 'name': r['name'], 'price': r['price'] or 0} for r in rows])


# ===== IMPORTAÇÃO DE PRODUTOS =====

@app.route('/products/import', methods=['GET', 'POST'])
def import_products():
    if request.method == 'POST':
        if 'file' not in request.files or request.files['file'].filename == '':
            flash('Selecione um arquivo Excel.', 'danger')
            return redirect(request.url)
        file = request.files['file']
        if not (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
            flash('Apenas arquivos .xlsx ou .xls são aceitos.', 'danger')
            return redirect(request.url)
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        try:
            df = pd.read_excel(filepath)
            df.columns = [str(c).strip().lower() for c in df.columns]
            col_map = {
                'sku': 'sku', 'cod': 'sku', 'código': 'sku', 'codigo': 'sku', 'code': 'sku',
                'ref': 'sku', 'referência': 'sku', 'referencia': 'sku',
                'nome': 'name', 'name': 'name', 'produto': 'name', 'product': 'name',
                'descrição do produto': 'name',
                'descrição': 'description', 'descricao': 'description', 'description': 'description',
                'desc': 'description', 'detalhe': 'description', 'detalhes': 'description',
                'preço': 'price', 'preco': 'price', 'price': 'price', 'valor': 'price',
                'preço de venda': 'price', 'preco de venda': 'price', 'venda': 'price',
            }
            df = df.rename(columns={c: col_map[c] for c in df.columns if c in col_map})
            if 'name' not in df.columns:
                flash('A planilha deve ter uma coluna "Nome" ou "Produto".', 'danger')
                return redirect(request.url)

            def safe(val):
                try:
                    if pd.isna(val):
                        return ''
                except Exception:
                    pass
                return '' if val is None else str(val).strip()

            def safe_price(val):
                try:
                    if pd.isna(val):
                        return 0.0
                    s = str(val).strip().replace('R$', '').replace('.', '').replace(',', '.').strip()
                    return float(s)
                except Exception:
                    return 0.0

            conn = get_db()
            imported = updated = errors = 0
            update_existing = request.form.get('update_existing') == '1'

            for _, row in df.iterrows():
                name = safe(row.get('name', ''))
                if not name:
                    errors += 1
                    continue
                sku = safe(row.get('sku', '')) or None
                description = safe(row.get('description', '')) or None
                price = safe_price(row.get('price', 0))

                if sku and update_existing:
                    existing = conn.execute('SELECT id FROM products WHERE sku=?', (sku,)).fetchone()
                    if existing:
                        conn.execute(
                            'UPDATE products SET name=?,description=?,price=? WHERE id=?',
                            (name, description, price, existing['id']))
                        updated += 1
                        continue

                if sku:
                    dup = conn.execute('SELECT id FROM products WHERE sku=?', (sku,)).fetchone()
                    if dup:
                        errors += 1
                        continue

                conn.execute(
                    'INSERT INTO products (sku, name, description, price) VALUES (?,?,?,?)',
                    (sku, name, description, price))
                imported += 1

            conn.commit()
            conn.close()
            os.remove(filepath)
            parts = [f'{imported} produto(s) importado(s)']
            if updated:
                parts.append(f'{updated} atualizado(s)')
            if errors:
                parts.append(f'{errors} linha(s) ignorada(s)')
            flash(' | '.join(parts), 'success')
        except Exception as e:
            flash(f'Erro ao processar arquivo: {e}', 'danger')
        return redirect(url_for('products_list'))
    return render_template('products/import.html')


@app.route('/products/import/template')
def import_products_template():
    output = io.BytesIO()
    df = pd.DataFrame([
        {'SKU': 'PROD-001', 'Nome': 'Consultoria Básica', 'Descrição': 'Sessão de 1 hora de consultoria', 'Preço': 350.00},
        {'SKU': 'PROD-002', 'Nome': 'Treinamento Online', 'Descrição': 'Curso completo gravado', 'Preço': 197.00},
        {'SKU': 'PROD-003', 'Nome': 'Pacote Premium', 'Descrição': 'Acesso completo à plataforma', 'Preço': 997.00},
    ])
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Produtos')
    output.seek(0)
    return send_file(output, download_name='modelo_produtos.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ===== IMPORTAÇÃO DE VENDAS =====

@app.route('/sales/import', methods=['GET', 'POST'])
def import_sales():
    results = None
    if request.method == 'POST':
        if 'file' not in request.files or request.files['file'].filename == '':
            flash('Selecione um arquivo Excel.', 'danger')
            return render_template('sales/import.html', results=None)
        file = request.files['file']
        if not (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
            flash('Apenas arquivos .xlsx ou .xls são aceitos.', 'danger')
            return render_template('sales/import.html', results=None)
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        try:
            df = pd.read_excel(filepath)
            df.columns = [str(c).strip().lower() for c in df.columns]
            col_map = {
                'id do cliente': 'client_ref', 'id cliente': 'client_ref',
                'cliente': 'client_ref', 'client': 'client_ref',
                'nome do cliente': 'client_ref', 'nome': 'client_ref',
                'sku': 'sku', 'sku do produto': 'sku', 'código': 'sku',
                'codigo': 'sku', 'cod': 'sku',
                'quantidade': 'quantity', 'qtd': 'quantity', 'qtde': 'quantity',
                'qty': 'quantity', 'quantity': 'quantity',
                'valor unitário': 'unit_price', 'valor unitario': 'unit_price',
                'preço': 'unit_price', 'preco': 'unit_price', 'valor': 'unit_price',
                'unit price': 'unit_price', 'valor unit': 'unit_price',
                'data da venda': 'sale_date', 'data': 'sale_date',
                'date': 'sale_date', 'data venda': 'sale_date',
            }
            df = df.rename(columns={c: col_map[c] for c in df.columns if c in col_map})

            required = ['client_ref', 'sku', 'unit_price', 'sale_date']
            col_labels = {
                'client_ref': 'ID do Cliente',
                'sku': 'SKU do Produto',
                'unit_price': 'Valor Unitário',
                'sale_date': 'Data da Venda',
            }
            missing = [col_labels[r] for r in required if r not in df.columns]
            if missing:
                flash(f'Colunas obrigatórias ausentes: {", ".join(missing)}. Baixe o modelo para ver o formato correto.', 'danger')
                os.remove(filepath)
                return render_template('sales/import.html', results=None)

            def safe(val):
                try:
                    if pd.isna(val):
                        return ''
                except Exception:
                    pass
                return '' if val is None else str(val).strip()

            def safe_float(val, default=0.0):
                try:
                    if pd.isna(val):
                        return default
                    if isinstance(val, (int, float)):
                        return float(val)
                    s = str(val).strip().replace('R$', '').strip()
                    if ',' in s and '.' in s:
                        s = s.replace('.', '').replace(',', '.')
                    elif ',' in s:
                        s = s.replace(',', '.')
                    return float(s)
                except Exception:
                    return default

            def parse_date(val):
                if val is None:
                    return None
                s = str(val).strip()
                for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d'):
                    try:
                        return datetime.strptime(s[:10], fmt).strftime('%Y-%m-%d')
                    except Exception:
                        pass
                try:
                    ts = pd.to_datetime(val)
                    return ts.strftime('%Y-%m-%d')
                except Exception:
                    return None

            conn = get_db()
            rows_result = []
            success_count = 0
            error_count = 0

            for idx, row in df.iterrows():
                row_num = idx + 2
                client_ref = safe(row.get('client_ref', ''))
                sku_val = safe(row.get('sku', ''))
                quantity = safe_float(row.get('quantity', 1), default=1.0) or 1.0
                unit_price = safe_float(row.get('unit_price', 0))
                sale_date = parse_date(row.get('sale_date'))

                if not client_ref:
                    rows_result.append({'row': row_num, 'client_ref': '—', 'sku': sku_val,
                                        'status': 'error', 'message': 'Cliente não informado'})
                    error_count += 1
                    continue

                # Find client: by code first, then exact name, then partial name
                client = None
                if client_ref.upper().startswith('CLI-'):
                    client = conn.execute(
                        'SELECT id, name, client_code FROM clients WHERE client_code = ?',
                        (client_ref.upper(),)).fetchone()
                if not client:
                    client = conn.execute(
                        'SELECT id, name, client_code FROM clients WHERE LOWER(name) = ?',
                        (client_ref.lower(),)).fetchone()
                if not client:
                    client = conn.execute(
                        'SELECT id, name, client_code FROM clients WHERE LOWER(name) LIKE ?',
                        (f'%{client_ref.lower()}%',)).fetchone()

                if not client:
                    rows_result.append({'row': row_num, 'client_ref': client_ref, 'sku': sku_val,
                                        'status': 'error',
                                        'message': f'Cliente não encontrado: "{client_ref}"'})
                    error_count += 1
                    continue

                # Find product by SKU, then by name
                product = None
                if sku_val:
                    product = conn.execute(
                        'SELECT id, name, price FROM products WHERE sku = ?', (sku_val,)).fetchone()
                    if not product:
                        product = conn.execute(
                            'SELECT id, name, price FROM products WHERE UPPER(sku) = ?',
                            (sku_val.upper(),)).fetchone()
                    if not product:
                        product = conn.execute(
                            'SELECT id, name, price FROM products WHERE LOWER(name) = ?',
                            (sku_val.lower(),)).fetchone()

                if not product:
                    rows_result.append({'row': row_num, 'client_ref': client['name'], 'sku': sku_val,
                                        'status': 'error',
                                        'message': f'Produto não encontrado (SKU: "{sku_val}")'})
                    error_count += 1
                    continue

                if not sale_date:
                    rows_result.append({'row': row_num, 'client_ref': client['name'], 'sku': sku_val,
                                        'status': 'error', 'message': 'Data da venda inválida ou ausente'})
                    error_count += 1
                    continue

                if unit_price <= 0:
                    rows_result.append({'row': row_num, 'client_ref': client['name'], 'sku': sku_val,
                                        'status': 'error',
                                        'message': 'Valor unitário deve ser maior que zero'})
                    error_count += 1
                    continue

                subtotal = round(quantity * unit_price, 2)
                sale_code = next_sale_code(conn)

                cur = conn.execute(
                    'INSERT INTO purchases (sale_code,client_id,product_id,product_name,value,purchase_date) VALUES (?,?,?,?,?,?)',
                    (sale_code, client['id'], product['id'], product['name'], subtotal, sale_date))
                purchase_id = cur.lastrowid
                conn.execute(
                    'INSERT INTO purchase_items (purchase_id,product_id,product_name,quantity,unit_price,subtotal) VALUES (?,?,?,?,?,?)',
                    (purchase_id, product['id'], product['name'], quantity, unit_price, subtotal))
                conn.execute(
                    "UPDATE clients SET type='active' WHERE id=? AND type='potential'",
                    (client['id'],))
                conn.commit()

                value_fmt = f'{subtotal:.2f}'.replace('.', ',')
                rows_result.append({
                    'row': row_num,
                    'client_ref': client['name'],
                    'sku': sku_val,
                    'status': 'success',
                    'message': f'{sale_code} — {product["name"]} x{quantity:.0f} = R$ {value_fmt}'
                })
                success_count += 1

            conn.close()
            os.remove(filepath)
            results = {
                'rows': rows_result,
                'success_count': success_count,
                'error_count': error_count,
            }
        except Exception as e:
            flash(f'Erro ao processar arquivo: {e}', 'danger')
        return render_template('sales/import.html', results=results)
    return render_template('sales/import.html', results=None)


@app.route('/sales/import/template')
def import_sales_template():
    output = io.BytesIO()
    df = pd.DataFrame([
        {'ID do Cliente': 'CLI-0001', 'SKU do Produto': 'PROD-001',
         'Quantidade': 1, 'Valor Unitário': 350.00, 'Data da Venda': '01/06/2025'},
        {'ID do Cliente': 'Maria Silva', 'SKU do Produto': 'PROD-002',
         'Quantidade': 2, 'Valor Unitário': 197.00, 'Data da Venda': '15/06/2025'},
        {'ID do Cliente': 'CLI-0003', 'SKU do Produto': 'PROD-003',
         'Quantidade': 1, 'Valor Unitário': 997.00, 'Data da Venda': '20/06/2025'},
    ])
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Vendas')
    output.seek(0)
    return send_file(output, download_name='modelo_vendas.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ===== BUSCA GLOBAL =====

@app.route('/search')
def global_search():
    q = request.args.get('q', '').strip()
    if not q:
        return redirect(url_for('dashboard'))

    conn = get_db()

    # CLI-XXXX → client detail
    if q.upper().startswith('CLI-'):
        client = conn.execute(
            'SELECT id FROM clients WHERE client_code = ?', (q.upper(),)).fetchone()
        conn.close()
        if client:
            return redirect(url_for('client_detail', cid=client['id']))
        flash(f'Nenhum cliente encontrado com o ID "{q.upper()}".', 'danger')
        return redirect(url_for('clients_list'))

    # VND-XXXX → purchase → client detail (anchored)
    if q.upper().startswith('VND-'):
        purchase = conn.execute(
            'SELECT id, client_id FROM purchases WHERE sale_code = ?', (q.upper(),)).fetchone()
        conn.close()
        if purchase:
            return redirect(url_for('client_detail', cid=purchase['client_id']) + f'#pu{purchase["id"]}')
        flash(f'Nenhuma venda encontrada com o ID "{q.upper()}".', 'danger')
        return redirect(url_for('dashboard'))

    # SKU → product edit
    product = conn.execute('SELECT id FROM products WHERE sku = ?', (q,)).fetchone()
    if not product:
        product = conn.execute(
            'SELECT id FROM products WHERE UPPER(sku) = ?', (q.upper(),)).fetchone()
    conn.close()
    if product:
        return redirect(url_for('product_edit', pid=product['id']))

    flash(
        f'Nenhum resultado para "{q}". '
        'Use ID de cliente (CLI-XXXX), ID de venda (VND-XXXX) ou SKU de produto.',
        'warning')
    return redirect(url_for('dashboard'))


# ===== COMPRAS =====

@app.route('/purchases/new', methods=['GET', 'POST'])
def purchase_new():
    conn = get_db()
    if request.method == 'POST':
        client_id = request.form.get('client_id', '').strip()
        items_raw = request.form.get('items_json', '[]')
        try:
            items = json.loads(items_raw)
        except Exception:
            items = []
        items = [i for i in items if str(i.get('product_name', '')).strip()]

        if not client_id or not items:
            flash('Cliente e pelo menos um produto são obrigatórios.', 'danger')
            clients = conn.execute('SELECT id, name FROM clients ORDER BY name').fetchall()
            conn.close()
            return render_template('purchases/form.html', clients=clients,
                                   selected_client=request.form.get('client_id', ''),
                                   today=datetime.now().strftime('%Y-%m-%d'))

        purchase_date = request.form.get('purchase_date') or datetime.now().strftime('%Y-%m-%d')
        notes = request.form.get('notes', '').strip()
        total = sum(float(i.get('subtotal', 0) or 0) for i in items)
        summary_name = ', '.join(str(i['product_name']) for i in items)
        first_pid = items[0].get('product_id') if len(items) == 1 else None
        sale_code = next_sale_code(conn)

        cur = conn.execute(
            'INSERT INTO purchases (sale_code,client_id,product_id,product_name,value,purchase_date,notes) VALUES (?,?,?,?,?,?,?)',
            (sale_code, client_id, first_pid, summary_name, total, purchase_date, notes))
        purchase_id = cur.lastrowid

        for item in items:
            qty = float(item.get('quantity', 1) or 1)
            unit = float(item.get('unit_price', 0) or 0)
            sub = float(item.get('subtotal', 0) or 0)
            conn.execute(
                'INSERT INTO purchase_items (purchase_id,product_id,product_name,quantity,unit_price,subtotal) VALUES (?,?,?,?,?,?)',
                (purchase_id, item.get('product_id') or None, str(item['product_name']).strip(), qty, unit, sub))

        conn.execute("UPDATE clients SET type='active' WHERE id=? AND type='potential'", (client_id,))
        conn.commit()
        next_page = request.form.get('next', '')
        conn.close()
        flash(f'Venda {sale_code} registrada com sucesso!', 'success')
        if next_page == 'client':
            return redirect(url_for('client_detail', cid=client_id))
        return redirect(url_for('dashboard'))

    selected_client = request.args.get('client_id', '')
    clients = conn.execute('SELECT id, name FROM clients ORDER BY name').fetchall()
    conn.close()
    return render_template('purchases/form.html', clients=clients,
                           selected_client=selected_client,
                           today=datetime.now().strftime('%Y-%m-%d'))


@app.route('/purchases/<int:pid>/delete', methods=['POST'])
def purchase_delete(pid):
    conn = get_db()
    pu = conn.execute('SELECT client_id FROM purchases WHERE id=?', (pid,)).fetchone()
    client_id = pu['client_id'] if pu else None
    conn.execute('DELETE FROM purchase_items WHERE purchase_id=?', (pid,))
    conn.execute('DELETE FROM purchases WHERE id=?', (pid,))
    conn.commit()
    conn.close()
    flash('Compra removida.', 'success')
    if client_id:
        return redirect(url_for('client_detail', cid=client_id))
    return redirect(url_for('dashboard'))


# ===== CRM =====

WHATSAPP_TEMPLATES = [
    {'name': 'boas_vindas', 'title': '👋 Boas-vindas',
     'message': 'Olá {nome}! 😊 Obrigado por se cadastrar conosco! Estamos muito felizes em tê-lo(a) como cliente. Pode contar com a gente para o que precisar!'},
    {'name': 'acompanhamento', 'title': '📞 Acompanhamento',
     'message': 'Olá {nome}, tudo bem? 👋 Gostaria de saber se você teve a oportunidade de pensar em nossa proposta. Estou à disposição para tirar qualquer dúvida. Quando seria um bom momento para conversarmos?'},
    {'name': 'agradecimento', 'title': '🎉 Agradecimento de Compra',
     'message': 'Olá {nome}! 🎉 Muito obrigado pela sua compra! Esperamos que fique muito satisfeito(a). Se precisar de qualquer coisa, estamos aqui. Foi um prazer atendê-lo(a)!'},
    {'name': 'promocao', 'title': '🌟 Promoção Especial',
     'message': 'Olá {nome}! 🌟 Temos novidades e promoções especiais para você! Aproveite nossas condições exclusivas. Entre em contato e saiba mais. Não perca essa oportunidade! 😊'},
    {'name': 'reengajamento', 'title': '💙 Reengajamento',
     'message': 'Olá {nome}! 😊 Sentimos sua falta! Faz um tempo que não nos falamos e gostaríamos de saber como você está. Temos novidades que podem ser do seu interesse. Podemos conversar?'},
    {'name': 'aniversario', 'title': '🎂 Aniversário',
     'message': 'Feliz aniversário, {nome}! 🎂🎊 Desejamos um dia muito especial cheio de alegria e realizações! Como presente especial, temos uma surpresa aguardando por você. Entre em contato!'},
]


@app.route('/crm')
def crm_index():
    search = request.args.get('search', '').strip()
    ctype = request.args.get('type', '')
    conn = get_db()
    q = '''
        SELECT c.id, c.client_code, c.name, c.phone, c.whatsapp, c.instagram, c.type,
               c.last_contact, c.notes,
               COUNT(p.id) AS buy_count,
               COALESCE(SUM(p.value), 0) AS total_spent,
               CASE WHEN c.last_contact IS NULL THEN NULL
                    ELSE CAST(julianday('now') - julianday(c.last_contact) AS INTEGER)
               END AS days_since
        FROM clients c LEFT JOIN purchases p ON c.id = p.client_id WHERE 1=1
    '''
    params = []
    if search:
        q += ' AND (c.name LIKE ? OR c.phone LIKE ? OR c.whatsapp LIKE ? OR c.client_code LIKE ?)'
        params += [f'%{search}%'] * 4
    if ctype in ('active', 'potential'):
        q += ' AND c.type = ?'
        params.append(ctype)
    q += ''' GROUP BY c.id
             ORDER BY CASE WHEN c.last_contact IS NULL THEN 0 ELSE 1 END,
                      c.last_contact ASC, c.name'''
    clients = conn.execute(q, params).fetchall()
    conn.close()
    return render_template('crm/index.html',
        clients=clients, templates=WHATSAPP_TEMPLATES, search=search, ctype=ctype)


@app.route('/crm/contact', methods=['POST'])
def crm_contact():
    client_id = request.form.get('client_id')
    if client_id:
        conn = get_db()
        conn.execute(
            'INSERT INTO contact_history (client_id, template_name, message) VALUES (?,?,?)',
            (client_id, request.form.get('template_name', ''), request.form.get('message', '')))
        conn.execute("UPDATE clients SET last_contact=DATE('now') WHERE id=?", (client_id,))
        conn.commit()
        conn.close()
        flash('Contato registrado com sucesso!', 'success')
    return redirect(url_for('crm_index'))


# ===== IMPORTAÇÃO DE CLIENTES =====

@app.route('/import', methods=['GET', 'POST'])
def import_clients():
    if request.method == 'POST':
        if 'file' not in request.files or request.files['file'].filename == '':
            flash('Selecione um arquivo Excel.', 'danger')
            return redirect(request.url)
        file = request.files['file']
        if not (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
            flash('Apenas arquivos .xlsx ou .xls são aceitos.', 'danger')
            return redirect(request.url)
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        try:
            df = pd.read_excel(filepath)
            df.columns = [str(c).strip().lower() for c in df.columns]
            col_map = {
                'nome': 'name', 'name': 'name', 'cliente': 'name',
                'telefone': 'phone', 'fone': 'phone', 'cel': 'phone', 'celular': 'phone', 'phone': 'phone',
                'whatsapp': 'whatsapp', 'wpp': 'whatsapp', 'zap': 'whatsapp',
                'instagram': 'instagram', 'insta': 'instagram',
                'facebook': 'facebook', 'fb': 'facebook',
                'tipo': 'type', 'type': 'type',
                'observações': 'notes', 'observacoes': 'notes', 'obs': 'notes',
                'notes': 'notes', 'notas': 'notes',
            }
            df = df.rename(columns={c: col_map[c] for c in df.columns if c in col_map})
            if 'name' not in df.columns:
                flash('A planilha deve ter uma coluna "Nome".', 'danger')
                return redirect(request.url)

            def safe(val):
                try:
                    if pd.isna(val):
                        return ''
                except Exception:
                    pass
                return '' if val is None else str(val).strip()

            conn = get_db()
            imported = errors = 0
            for _, row in df.iterrows():
                name = safe(row.get('name', ''))
                if not name:
                    errors += 1
                    continue
                t = safe(row.get('type', '')).lower()
                client_type = 'active' if t in ('active', 'ativo', 'a') else 'potential'
                code = next_client_code(conn)
                conn.execute(
                    'INSERT INTO clients (client_code,name,phone,whatsapp,instagram,facebook,type,notes) VALUES (?,?,?,?,?,?,?,?)',
                    (code, name, safe(row.get('phone', '')), safe(row.get('whatsapp', '')),
                     safe(row.get('instagram', '')), safe(row.get('facebook', '')),
                     client_type, safe(row.get('notes', ''))))
                imported += 1
            conn.commit()
            conn.close()
            os.remove(filepath)
            flash(f'{imported} clientes importados com sucesso! ({errors} linhas ignoradas)', 'success')
        except Exception as e:
            flash(f'Erro ao processar arquivo: {e}', 'danger')
        return redirect(url_for('clients_list'))
    return render_template('import.html')


@app.route('/import/template')
def import_template():
    output = io.BytesIO()
    df = pd.DataFrame([{
        'Nome': 'Maria Silva', 'Telefone': '11987654321', 'WhatsApp': '11987654321',
        'Instagram': '@maria.silva', 'Facebook': 'maria.silva',
        'Tipo': 'potential', 'Observações': 'Interessada no produto X'
    }])
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Clientes')
    output.seek(0)
    return send_file(output, download_name='modelo_clientes.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000, host='0.0.0.0')
