"""古诗词查询 MCP Server（FastMCP 简化模式，stdio 传输）。

提供三个工具：
- search_poetry：按关键词在标题 / 内容中检索；
- random_poetry：随机返回一首；
- list_poets：列出全部诗人（去重）。

直接运行（被 MCP 客户端以 stdio 子进程方式拉起）：
    python mcp_servers/poetry_server.py

数据为代码内硬编码的诗词列表，无外部网络依赖。
"""

import random

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("poetry-server")

POEMS = [
    {"title": "静夜思", "author": "李白", "dynasty": "唐",
     "content": "床前明月光，疑是地上霜。举头望明月，低头思故乡。"},
    {"title": "春晓", "author": "孟浩然", "dynasty": "唐",
     "content": "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。"},
    {"title": "登鹳雀楼", "author": "王之涣", "dynasty": "唐",
     "content": "白日依山尽，黄河入海流。欲穷千里目，更上一层楼。"},
    {"title": "望庐山瀑布", "author": "李白", "dynasty": "唐",
     "content": "日照香炉生紫烟，遥看瀑布挂前川。飞流直下三千尺，疑是银河落九天。"},
    {"title": "绝句", "author": "杜甫", "dynasty": "唐",
     "content": "两个黄鹂鸣翠柳，一行白鹭上青天。窗含西岭千秋雪，门泊东吴万里船。"},
    {"title": "枫桥夜泊", "author": "张继", "dynasty": "唐",
     "content": "月落乌啼霜满天，江枫渔火对愁眠。姑苏城外寒山寺，夜半钟声到客船。"},
    {"title": "游子吟", "author": "孟郊", "dynasty": "唐",
     "content": "慈母手中线，游子身上衣。临行密密缝，意恐迟迟归。谁言寸草心，报得三春晖。"},
    {"title": "江雪", "author": "柳宗元", "dynasty": "唐",
     "content": "千山鸟飞绝，万径人踪灭。孤舟蓑笠翁，独钓寒江雪。"},
    {"title": "赋得古原草送别", "author": "白居易", "dynasty": "唐",
     "content": "离离原上草，一岁一枯荣。野火烧不尽，春风吹又生。"},
    {"title": "水调歌头·明月几时有", "author": "苏轼", "dynasty": "宋",
     "content": "明月几时有？把酒问青天。不知天上宫阙，今夕是何年。"
                "我欲乘风归去，又恐琼楼玉宇，高处不胜寒。起舞弄清影，何似在人间。"
                "转朱阁，低绮户，照无眠。不应有恨，何事长向别时圆？"
                "人有悲欢离合，月有阴晴圆缺，此事古难全。但愿人长久，千里共婵娟。"},
    {"title": "念奴娇·赤壁怀古", "author": "苏轼", "dynasty": "宋",
     "content": "大江东去，浪淘尽，千古风流人物。故垒西边，人道是，三国周郎赤壁。"
                "乱石穿空，惊涛拍岸，卷起千堆雪。江山如画，一时多少豪杰。"
                "遥想公瑾当年，小乔初嫁了，雄姿英发。羽扇纶巾，谈笑间，樯橹灰飞烟灭。"
                "故国神游，多情应笑我，早生华发。人生如梦，一尊还酹江月。"},
    {"title": "如梦令", "author": "李清照", "dynasty": "宋",
     "content": "昨夜雨疏风骤，浓睡不消残酒。试问卷帘人，却道海棠依旧。"
                "知否，知否？应是绿肥红瘦。"},
    {"title": "登飞来峰", "author": "王安石", "dynasty": "宋",
     "content": "飞来山上千寻塔，闻说鸡鸣见日升。不畏浮云遮望眼，自缘身在最高层。"},
]


def _format_poem(poem: dict) -> str:
    """统一输出格式：《标题》—— 作者（朝代）换行接内容。"""
    return f"《{poem['title']}》—— {poem['author']}（{poem['dynasty']}）\n{poem['content']}"


@mcp.tool()
def search_poetry(keyword: str) -> str:
    """搜索包含指定关键词的古诗词（在标题和内容中匹配），返回全部命中的诗。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return "请提供要搜索的关键词"

    matched = [
        poem for poem in POEMS
        if keyword in poem["title"] or keyword in poem["content"]
    ]
    if not matched:
        return "未找到包含该关键词的诗词"

    return "\n\n".join(_format_poem(poem) for poem in matched)


@mcp.tool()
def random_poetry() -> str:
    """随机返回一首古诗词。"""
    return _format_poem(random.choice(POEMS))


@mcp.tool()
def list_poets() -> str:
    """返回诗词库中所有诗人的列表（按首次出现顺序去重）。"""
    poets: list[str] = []
    for poem in POEMS:
        if poem["author"] not in poets:
            poets.append(poem["author"])
    return "、".join(poets) + f"（共 {len(poets)} 位）"


if __name__ == "__main__":
    mcp.run(transport="stdio")
