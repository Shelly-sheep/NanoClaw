---
name: csv_file_read
description: 自动识别分隔符和编码，将CSV转换为结构化数据，支持清洗与统计
---

# CSV 数据分析技能

自动识别 CSV 文件的分隔符和编码，将数据转换为结构化格式，支持数据清洗与统计分析。

## 使用方法

### 第一步：确认文件位置

调用 `list_dir` 查看工作区内的 CSV 文件：

```
工具：list_dir
参数：path = "."
```

找到目标 `.csv` 文件后，记下其相对路径。

### 第二步：探测文件编码和分隔符

调用 `exec` 用 Python 探测编码和分隔符：

```
工具：exec
参数：command = "python3 -c \"
import csv, chardet
with open('data.csv', 'rb') as f:
    raw = f.read(8192)
    enc = chardet.detect(raw)
    print('编码:', enc)
    sniff = csv.Sniffer().sniff(raw.decode(enc['encoding'][:20]))
    print('分隔符:', repr(sniff.delimiter))
\""
```

- 如果 `chardet` 未安装，先执行 `pip install chardet`。
- 常见编码：`utf-8`、`gbk`、`gb2312`、`latin-1`。
- 常见分隔符：逗号 `,`、制表符 `\t`、分号 `;`、竖线 `|`。

### 第三步：预览数据

调用 `exec` 用 Python 读取前 5 行，确认列名和数据样貌：

```
工具：exec
参数：command = "python3 -c \"
import csv
with open('data.csv', encoding='utf-8') as f:
    reader = csv.reader(f)
    for i, row in enumerate(reader):
        if i >= 6: break
        print(row)
\""
```

- 如果编码不是 utf-8，替换为上一步探测到的编码。
- 如果分隔符不是逗号，加上 `delimiter='\\t'` 等参数。

### 第四步：数据清洗

调用 `exec` 执行清洗脚本。常见操作：

```python
import csv

rows = []
with open('data.csv', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        # 去除首尾空白
        row = {k.strip(): v.strip() for k, v in row.items() if k}
        # 跳过空行
        if any(row.values()):
            rows.append(row)

print(f'有效行数: {len(rows)}')
print(f'列名: {list(rows[0].keys())}' if rows else '无数据')
```

- 数值列需要去单位、转类型时，在循环中加 `float(v.replace('¥','').replace(',',''))`。
- 日期列需要统一格式时，用 `datetime.strptime(v, '%Y/%m/%d')`。

### 第五步：统计分析

调用 `exec` 执行统计脚本：

```python
import csv
from collections import Counter

rows = []
with open('data.csv', encoding='utf-8') as f:
    for row in csv.DictReader(f):
        rows.append({k.strip(): v.strip() for k, v in row.items() if k})

# 基础统计
print(f'总行数: {len(rows)}')

# 按某列分组计数
group_col = 'category'  # 替换为实际列名
counter = Counter(r.get(group_col, '') for r in rows)
for k, v in counter.most_common(10):
    print(f'  {k}: {v}')

# 数值列统计
num_col = 'price'  # 替换为实际列名
nums = []
for r in rows:
    try:
        nums.append(float(r[num_col]))
    except (ValueError, KeyError):
        pass
if nums:
    print(f'{num_col} - 总和: {sum(nums):.2f}, 平均: {sum(nums)/len(nums):.2f}, 最大: {max(nums):.2f}, 最小: {min(nums):.2f}')
```

### 第六步：保存结果（可选）

如果需要保存清洗后的数据或统计结果，调用 `write_file`：

```
工具：write_file
参数：path = "cleaned_data.csv", content = "<清洗后的 CSV 内容>"
```

或用 `exec` 直接在 Python 脚本中写文件。

## 示例

用户：帮我分析 sales.csv 这个文件

助手：好的，我先看看文件。

（调用 list_dir，path="."）

找到 `sales.csv`，接下来探测编码和分隔符。

（调用 exec，执行 chardet + csv.Sniffer 探测脚本）

探测结果：编码 utf-8，分隔符 `,`。现在预览数据。

（调用 exec，读取前 5 行）

列名：`日期, 产品, 销量, 单价, 总额`，共 5 列。开始清洗和统计。

（调用 exec，执行清洗 + 统计脚本）

结果：
- 总行数：320（清洗后 318，去除 2 个空行）
- 销量：总和 12,450，平均 39.15，最大 200，最小 1
- 按产品分组：产品A (120 笔)，产品B (98 笔)，产品C (100 笔)

需要我把清洗后的数据保存为新文件吗？

## 注意事项

- **编码问题**：如果 utf-8 读取报错，先探测编码。GBK/GB2312 在中文 Windows 导出的 CSV 中很常见。
- **分隔符变体**：有些 CSV 用分号 `;` 或制表符 `\t`，`csv.Sniffer` 不一定准确，预览时人工确认。
- **大文件**：超过 10 万行的文件，统计时避免 `readlines()` 一次性加载，用迭代器逐行处理。
- **列名不一致**：可能有 BOM 头（`\ufeff`），导致第一列名带隐藏字符，`strip()` 可以清除。
- **数值列含单位**：如 `¥1,234.56` 或 `12.5%`，需在脚本中做字符串清洗后再转 `float`。
- **缺失值**：空字符串、`N/A`、`NULL` 等，统计时用 `try/except` 跳过或填默认值。
- **chardet 依赖**：如未安装，先 `exec` 执行 `pip install chardet`；或直接试 utf-8 再试 gbk。
