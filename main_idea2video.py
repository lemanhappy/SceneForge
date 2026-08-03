import sys
# Pipeline prints emoji progress markers; the Windows console codepage (e.g. GBK)
# cannot encode them and raises UnicodeEncodeError. Force UTF-8 on the streams.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import asyncio
from pipelines.idea2video_pipeline import Idea2VideoPipeline


# SET YOUR OWN IDEA, USER REQUIREMENT, AND STYLE HERE
idea = \
    """
王云宝是一个刚刚失业的普通中年男人，生活窘迫、心事重重。某天深夜，他在破旧的出租屋里
偶然得到一枚散发微光的神秘古玉，激活了体内沉睡的修仙天赋。从此他踏上逆袭之路：白天是
被生活压垮的落魄打工人，夜晚却在屋顶悄悄修炼，灵气环绕，掌心凝聚出法术之光，最终御剑
而起，俯瞰城市万家灯火，眼神重新燃起希望。
"""
user_requirement = \
    """
面向短视频观众，爽文逆袭风格，节奏明快、有冲击力。不超过2个场景，每个场景不超过2个镜头。
所有台词、独白、旁白用简体中文。
"""
style = "国风玄幻修仙，电影级画质，cinematic Chinese xianxia"


async def main():
    pipeline = Idea2VideoPipeline.init_from_config(
        config_path="configs/idea2video.yaml")
    await pipeline(idea=idea, user_requirement=user_requirement, style=style)

if __name__ == "__main__":
    asyncio.run(main())
