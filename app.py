from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import requests
import json
import os
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app)

# 腾讯表格配置
FILE_ID = "DRkR6aXhGcWxLYVFR"
SHEET_ID = "000007"       # 自助排队表格
MODEL_SHEET_ID = "000008"  # 牌号表格

# 腾讯开放平台配置
CLIENT_ID = os.environ.get('CLIENT_ID', 'da815d1227294457b43413bdc16e3e90')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjbHQiOiJkYTgxNWQxMjI3Mjk0NDU3YjQzNDEzYmRjMTZlM2U5MCIsInR5cCI6MSwiZXhwIjoxNzgyMDk0NTcyLjEwODc1MywiaWF0IjoxNzc5NTAyNTcyLjEwODc1Mywic3ViIjoiOWJjMTcyZTUzMzgxNDdkOGEzNWMxNDM4ZWE4ZDE1NzcifQ.rm3BIdD1V7FrCwdToT2arErs06xWF7hTqAh0KsCKsdw')
OPEN_ID = os.environ.get('OPEN_ID', '9bc172e5338147d8a35c1438ea8d1577')

BASE_URL = "https://docs.qq.com/openapi/spreadsheet/v3"


def get_headers():
    return {
        "Content-Type": "application/json",
        "Access-Token": ACCESS_TOKEN,
        "Open-Id": OPEN_ID,
        "Client-Id": CLIENT_ID
    }


def parse_cell_value(cell_value):
    """解析单元格值，统一返回字符串"""
    if not cell_value:
        return ""
    if "text" in cell_value:
        return cell_value["text"]
    if "number" in cell_value:
        return str(cell_value["number"])
    if "time" in cell_value:
        t = cell_value["time"]
        return f"{t['year']}-{t['month']:02d}-{t['day']:02d}"
    if "select" in cell_value:
        vals = cell_value["select"].get("value", [])
        return vals[0] if vals else ""
    if "link" in cell_value:
        return cell_value["link"].get("text", cell_value["link"].get("url", ""))
    return ""


def build_cell_value(value, is_date=False):
    """构建单元格写入值"""
    if is_date and value:
        try:
            parts = value.split("-")
            return {"cellValue": {"time": {
                "year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])
            }}}
        except:
            pass
    return {"cellValue": {"text": str(value) if value else ""}}


def read_sheet_range(sheet_id, range_str):
    """读取表格范围数据，返回gridData"""
    url = f"{BASE_URL}/files/{FILE_ID}/{sheet_id}/{range_str}"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        data = resp.json()
        return data.get("gridData", {})
    return {}


def get_row_count(sheet_id):
    """获取表格有效数据行数（基于A列型号列判断）"""
    grid_data = read_sheet_range(sheet_id, "A1:A1000")
    rows = grid_data.get("rows", [])
    last_row = 0
    for i, row in enumerate(rows):
        if i == 0:
            continue  # 跳过表头
        for v in row.get("values", []):
            cv = v.get("cellValue")
            if cv:
                text = parse_cell_value(cv)
                if text:
                    last_row = i + 1
                    break
    return last_row


def batch_update(requests_body):
    """执行批量更新操作"""
    url = f"{BASE_URL}/files/{FILE_ID}/batchUpdate"
    resp = requests.post(url, headers=get_headers(), json=requests_body)
    return resp


def write_order_row(row_index_0based, model, tonnage, customer, expected_date, queue_date, submitter, remark, serial_no, submitter_id, submit_time):
    """写入一行订单数据到腾讯表格（row_index_0based从0开始）"""
    values = [
        build_cell_value(model),
        build_cell_value(tonnage),
        build_cell_value(customer),
        build_cell_value(expected_date, is_date=True),
        build_cell_value(""),  # E列可发货日期 - 有公式保护，留空
        build_cell_value(queue_date, is_date=True),
        build_cell_value(submitter),
        build_cell_value(remark),
        build_cell_value(serial_no),
        build_cell_value(""),  # 上次录入
        build_cell_value(submitter_id),
        build_cell_value(submit_time),
    ]

    body = {
        "requests": [{
            "updateRangeRequest": {
                "sheetId": SHEET_ID,
                "gridData": {
                    "startRow": row_index_0based,
                    "startColumn": 0,
                    "rows": [{"values": values}]
                }
            }
        }]
    }
    return batch_update(body)


def delete_row(row_index_1based):
    """删除一行（row_index_1based从1开始）"""
    body = {
        "requests": [{
            "deleteDimensionRequest": {
                "sheetId": SHEET_ID,
                "dimension": "ROW",
                "startIndex": row_index_1based,
                "endIndex": row_index_1based + 1
            }
        }]
    }
    return batch_update(body)


# ==================== 路由 ====================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/models', methods=['GET'])
def get_models():
    """获取型号列表（从牌号表格A列）"""
    try:
        grid_data = read_sheet_range(MODEL_SHEET_ID, "A1:A100")
        rows = grid_data.get("rows", [])
        models = []
        for row in rows:
            for v in row.get("values", []):
                cv = v.get("cellValue")
                if cv:
                    text = parse_cell_value(cv)
                    if text:
                        models.append(text)
        return jsonify({"success": True, "models": models})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/calculate-date', methods=['POST'])
def calculate_date():
    """计算可发货日期：先写入数据到表格末尾（让E列公式自动计算），然后读取结果"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        customer = data.get('customer', '')
        expected_date = data.get('expected_date', '')

        # 获取当前最后一行（已有数据的最后一行）
        last_row = get_row_count(SHEET_ID)
        write_row_idx = last_row  # 0-based，写在最后一行之后（新行）
        serial_no = write_row_idx

        # 写入数据到表格末尾（让E列公式自动计算）
        # 排队日期、提交人等先留空，等用户正式提交时再更新
        remark = f"{tonnage}{customer}"
        resp = write_order_row(
            write_row_idx, model, tonnage, customer, expected_date,
            "", "", remark, str(serial_no), "", ""
        )
        result = resp.json()

        if "responses" not in result:
            return jsonify({"success": False, "error": f"写入数据失败: {json.dumps(result, ensure_ascii=False)}"})

        # 等待公式计算
        import time
        time.sleep(2)

        # 读取E列计算结果
        grid_data = read_sheet_range(SHEET_ID, f"E{write_row_idx + 1}:E{write_row_idx + 1}")
        rows = grid_data.get("rows", [])
        calculated_date = ""
        if rows:
            for v in rows[0].get("values", []):
                cv = v.get("cellValue")
                if cv:
                    calculated_date = parse_cell_value(cv)

        # 返回计算结果和行号，前端正式提交时用这个行号更新
        return jsonify({
            "success": True,
            "calculated_date": calculated_date,
            "row_index": write_row_idx + 1  # 1-based行号，供正式提交时更新
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders', methods=['POST'])
def create_order():
    """创建订单：如果有row_index则更新已有行，否则新建行"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        customer = data.get('customer', '')
        expected_date = data.get('expected_date', '')
        queue_date = data.get('queue_date', '')
        submitter = data.get('submitter', '未知用户')
        submitter_id = data.get('submitter_id', '')
        row_index = data.get('row_index', 0)  # 1-based，由calculate_date返回

        remark = f"{tonnage}{customer}"
        submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if row_index > 0:
            # 更新已有行（由calculate_date创建的行）
            write_row_idx = row_index - 1  # 转为0-based
            # 读取原序号，保持不变
            grid_data = read_sheet_range(SHEET_ID, f"I{row_index}:I{row_index}")
            rows = grid_data.get("rows", [])
            serial_no = str(write_row_idx)
            if rows:
                for v in rows[0].get("values", []):
                    cv = v.get("cellValue")
                    if cv:
                        serial_no = parse_cell_value(cv) or str(write_row_idx)
        else:
            # 新建行
            last_row = get_row_count(SHEET_ID)
            write_row_idx = last_row
            serial_no = str(write_row_idx)

        resp = write_order_row(
            write_row_idx, model, tonnage, customer, expected_date,
            queue_date, submitter, remark, serial_no, submitter_id, submit_time
        )
        result = resp.json()

        if "responses" in result:
            updated = result["responses"][0].get("updateRangeResponse", {}).get("updatedCells", 0)
            if updated > 0:
                return jsonify({"success": True, "message": "订单创建成功"})
            return jsonify({"success": False, "error": "写入0个单元格"})
        else:
            return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders', methods=['GET'])
def get_orders():
    """获取订单列表"""
    try:
        submitter_id = request.args.get('submitter_id', '')

        grid_data = read_sheet_range(SHEET_ID, "A1:L1000")
        rows = grid_data.get("rows", [])
        orders = []
        today = datetime.now().date()

        for i, row in enumerate(rows):
            if i == 0:
                continue  # 跳过表头

            values = row.get("values", [])
            if len(values) < 6:
                continue

            # 解析各列
            row_data = [parse_cell_value(v.get("cellValue")) for v in values]

            # 检查权限
            row_submitter_id = row_data[10] if len(row_data) > 10 else ""
            if submitter_id and row_submitter_id != submitter_id:
                continue

            # 检查排队日期是否过期
            queue_date_str = row_data[5] if len(row_data) > 5 else ""
            if queue_date_str:
                try:
                    queue_date = datetime.strptime(queue_date_str, "%Y-%m-%d").date()
                    if queue_date < today:
                        continue
                except:
                    pass

            # 至少有一个有效字段才显示
            if not any(row_data[:6]):
                continue

            order = {
                "row_index": i + 1,  # 1-based
                "model": row_data[0] if len(row_data) > 0 else "",
                "tonnage": row_data[1] if len(row_data) > 1 else "",
                "customer": row_data[2] if len(row_data) > 2 else "",
                "expected_date": row_data[3] if len(row_data) > 3 else "",
                "calculated_date": row_data[4] if len(row_data) > 4 else "",
                "queue_date": row_data[5] if len(row_data) > 5 else "",
                "submitter": row_data[6] if len(row_data) > 6 else "",
                "remark": row_data[7] if len(row_data) > 7 else "",
                "serial_no": row_data[8] if len(row_data) > 8 else "",
                "last_entry": row_data[9] if len(row_data) > 9 else "",
                "submitter_id": row_submitter_id,
                "submit_time": row_data[11] if len(row_data) > 11 else ""
            }
            orders.append(order)

        return jsonify({"success": True, "orders": orders})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders/<int:row_index>', methods=['PUT'])
def update_order(row_index):
    """修改订单"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        customer = data.get('customer', '')
        expected_date = data.get('expected_date', '')
        queue_date = data.get('queue_date', '')
        submitter = data.get('submitter', '')
        submitter_id = data.get('submitter_id', '')

        remark = f"{tonnage}{customer}"

        # 读取原订单检查吨位
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if rows:
            orig_values = [parse_cell_value(v.get("cellValue")) for v in rows[0].get("values", [])]
            original_tonnage = orig_values[1] if len(orig_values) > 1 else "0"
            try:
                if float(tonnage) > float(original_tonnage):
                    return jsonify({"success": False, "error": "吨位只能改小不能改大"})
            except ValueError:
                pass

        # 更新（row_index是1-based，转为0-based）
        write_idx = row_index - 1
        resp = write_order_row(
            write_idx, model, tonnage, customer, expected_date,
            queue_date, submitter, remark, str(write_idx), submitter_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        result = resp.json()

        if "responses" in result:
            return jsonify({"success": True, "message": "订单修改成功"})
        else:
            return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders/<int:row_index>', methods=['DELETE'])
def delete_order(row_index):
    """删除订单（row_index是1-based）"""
    try:
        resp = delete_row(row_index)
        result = resp.json()
        if "responses" in result:
            deleted = result["responses"][0].get("deleteDimensionResponse", {}).get("deleted", 0)
            if deleted > 0:
                return jsonify({"success": True, "message": "订单删除成功"})
        return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/test-connection', methods=['GET'])
def test_connection():
    """测试腾讯表格连接"""
    try:
        url = f"{BASE_URL}/files/{FILE_ID}"
        resp = requests.get(url, headers=get_headers())
        if resp.status_code == 200:
            data = resp.json()
            sheets = data.get("properties", [])
            sheet_names = [s["title"] for s in sheets]
            return jsonify({
                "success": True,
                "message": "连接成功",
                "sheets": sheet_names,
                "total_sheets": len(sheets)
            })
        else:
            return jsonify({"success": False, "error": f"连接失败，状态码: {resp.status_code}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
