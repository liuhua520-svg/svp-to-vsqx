#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
svp_to_vsqx.py —— SVP -> VSQX 转换器（保留 Group 原始位置版本）
=================================================================

核心结论（已用你提供的两个真实文件校验过，不是纯理论推导）：

    SVP 的每个 Group（track.mainRef 指向的内嵌组，或 track.groups[] 引用
    library[] 的可复用组）都用 blickAbsoluteBegin 决定它在时间轴上的绝对位置，
    组内每个 Note 的 onset 则是相对组起点的偏移。

    VSQX（vsq4）没有 Group 概念，但一个 vsTrack 下可以放多个 vsPart，每个
    vsPart 用 <t> 记录它在文件里的绝对 tick 位置（这个位置是"包含 preMeasure
    空白小节"的文件绝对坐标），Part 内每个 <note> 的 <t> 则是相对 Part 起点
    的偏移。

    两边"容器绝对位置 + 容器内相对位置"的两层结构是完全对应的，所以最简单、
    最不容易丢信息的做法就是 1:1 映射，不展开：

        SVP Group  ->  VSQX vsPart   （不拆散，不合并）
        Group.blickAbsoluteBegin  -> Part.<t>  （经过 tempo 换算 + preMeasure 偏移）
        Note.onset（相对 Group）   -> Note.<t> （相对 Part，同样经过 tempo 换算）

    这样每个 Group 的内部相对结构完整保留，后续做 VSQX -> SVP 的反向转换也
    容易很多。

本脚本对网上一份流传的转换说明做了两处关键纠正（已用真实文件核实）：

    1. vsq4（也就是 VSQX4 / Vocaloid4 用的 schema）里 Part 和 Note 的位置
       标签实际上是 <t>，不是 <posTick>。<posTick> 是另一个不同的 schema
       （vsqx3，Rust crate 文档里那个）用的叫法，vsq4 命名空间下根本没有
       这个标签，按那个改一定会导致文件打不开。
    2. SVP 的 blick 不能直接等价成 VSQX 的 tick，中间必须按 tempo 换算：
           1 blick = 1 / 705,600,000 秒   （SVP 的绝对时间精度单位，与 tempo 无关）
           tick = 已流逝秒数 * resolution * bpm / 60
       另外 VSQX 的 masterTrack.preMeasure（默认 4 小节空白前奏）会整体
       平移所有 Part 的绝对 <t>，比如这次给的模板文件 preMeasure=4、
       resolution=480、4/4 拍，算出来的空白前奏是 4*480*4 = 7680 tick，
       模板里现成的那个 vsPart 的 <t> 正好就是 7680 —— 说明"歌曲第一小节
       正拍"在文件里的绝对坐标就是 preMeasure_ticks，不加这个偏移的话
       所有音符会整体提前 4 小节。

用法示例：
    python svp_to_vsqx.py SAMPLE.svp -o output.vsqx
    python svp_to_vsqx.py SAMPLE.svp --template SAMPLE12345.vsqx -o output.vsqx

    --template 现在是可选参数：不提供时使用脚本内置的默认空白模板
    （resolution=480、preMeasure=4、4/4 拍、MIKU_V4X_Original 音色）；
    提供时则用来提供 VSQX 需要但 SVP 没有的信息：resolution、preMeasure、
    voice bank（sPlug / singer）、pStyle 等。转换时只替换 vsTrack 下的
    vsPart 内容（每个 SVP Group 一个 Part），masterTrack 的 tempo/timeSig
    会替换成 SVP 自己的 tempo/meter（这样输出文件的 tick 网格和用来计算
    tick 的 tempo 曲线是自洽的，不会因为模板和 SVP 拍速不一致而错位）。
    其余（vender/version/vVoiceTable/mixer/monoTrack/stTrack/aux 等）原样
    保留模板内容。

未覆盖的部分（有意保持这版脚本聚焦在"位置"这一个问题上）：
    - 音高曲线 / F0 -> <cc><v id="P"> 弯音曲线，没有生成（需要额外的 F0 数据
      源，和 USTX 的 pitd 曲线导出是同一类工作，可以按同样思路另外接一个
      F0 -> cc 的转换函数）。
    - 音素映射：SVP note.phonemes 为空时按"歌词=发音"处理（不加 lock），
      非空时原样写入并加 lock="1"。如果要做真正的 ARPABET -> VOCALOID4
      音素映射，建议复用你现有工程里那套映射表，这里只留了一个可以直接
      替换的钩子（见 build_note_element 里的 phoneme 处理逻辑）。
    - 多次拍号变化（time.meter 有多条记录）只取第一条，多余的会打印警告。
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import List, Optional

BLICKS_PER_SECOND = 1_411_200_000
# SVP 的绝对时间精度单位，恒定，与 tempo 无关。
#
# 注意：网上/社区常见资料写的是 705,600,000，之前几版脚本也一直用这个值，
# 但实测下来（用 DS.svp 及其对应的 Synthesizer V 截图逐像素核对：截图里第一个
# 音符高亮块宽度约 59px，小节标尺间距约 601px = 2 秒 @120bpm，换算出该音符
# 实际时长约 0.2 秒；而用 705,600,000 算出来的时长是 0.4 秒，正好是实测值的
# 2 倍）确认 705,600,000 是错的，正确值是它的 2 倍，也就是这里的
# 1,411,200,000。用错的常数会导致算出来的 tick 整体偏大 2 倍，表现为 VSQX
# 里的音符位置/间距相对 SVP 原始小节位置被拉伸了一倍。

NS_URI = "http://www.yamaha.co.jp/vocaloid/schema/vsq4/"

# 内置的默认 VSQX 模板（不提供 --template 时使用），结构上是一个干净的、
# 空的 vsq4 工程文件（resolution=480、preMeasure=4、4/4 拍、MIKU_V4X_Original
# 音色），只用来提供 VSQX 需要但 SVP 没有的结构信息，本身不含任何真实音符。
DEFAULT_TEMPLATE_TEXT = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<vsq4 xmlns="http://www.yamaha.co.jp/vocaloid/schema/vsq4/"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:schemaLocation="http://www.yamaha.co.jp/vocaloid/schema/vsq4/ vsq4.xsd">
\t<vender><![CDATA[Yamaha corporation]]></vender>
\t<version><![CDATA[4.0.0.3]]></version>
\t<vVoiceTable>
\t\t<vVoice>
\t\t\t<bs>0</bs>
\t\t\t<pc>52</pc>
\t\t\t<id><![CDATA[BCNFCY43LB2LZCD4]]></id>
\t\t\t<name><![CDATA[MIKU_V4X_Original_EVEC]]></name>
\t\t\t<vPrm>
\t\t\t\t<bre>0</bre>
\t\t\t\t<bri>0</bri>
\t\t\t\t<cle>0</cle>
\t\t\t\t<gen>0</gen>
\t\t\t\t<ope>0</ope>
\t\t\t</vPrm>
\t\t</vVoice>
\t</vVoiceTable>
\t<mixer>
\t\t<masterUnit>
\t\t\t<oDev>0</oDev>
\t\t\t<rLvl>0</rLvl>
\t\t\t<vol>0</vol>
\t\t</masterUnit>
\t\t<vsUnit>
\t\t\t<tNo>0</tNo>
\t\t\t<iGin>0</iGin>
\t\t\t<sLvl>-898</sLvl>
\t\t\t<sEnable>0</sEnable>
\t\t\t<m>0</m>
\t\t\t<s>0</s>
\t\t\t<pan>64</pan>
\t\t\t<vol>0</vol>
\t\t</vsUnit>
\t\t<monoUnit>
\t\t\t<iGin>0</iGin>
\t\t\t<sLvl>-898</sLvl>
\t\t\t<sEnable>0</sEnable>
\t\t\t<m>0</m>
\t\t\t<s>0</s>
\t\t\t<pan>64</pan>
\t\t\t<vol>0</vol>
\t\t</monoUnit>
\t\t<stUnit>
\t\t\t<iGin>0</iGin>
\t\t\t<m>0</m>
\t\t\t<s>0</s>
\t\t\t<vol>-129</vol>
\t\t</stUnit>
\t</mixer>
\t<masterTrack>
\t\t<seqName><![CDATA[Untitled10]]></seqName>
\t\t<comment><![CDATA[New VSQ File]]></comment>
\t\t<resolution>480</resolution>
\t\t<preMeasure>4</preMeasure>
\t\t<timeSig><m>0</m><nu>4</nu><de>4</de></timeSig>
\t\t<tempo><t>0</t><v>12000</v></tempo>
\t</masterTrack>
\t<vsTrack>
\t\t<tNo>0</tNo>
\t\t<name><![CDATA[Track]]></name>
\t\t<comment><![CDATA[Track]]></comment>
\t\t<vsPart>
\t\t\t<t>7680</t>
\t\t\t<playTime>61440</playTime>
\t\t\t<name><![CDATA[NewPart]]></name>
\t\t\t<comment><![CDATA[New Musical Part]]></comment>
\t\t\t<sPlug>
\t\t\t\t<id><![CDATA[ACA9C502-A04B-42b5-B2EB-5CEA36D16FCE]]></id>
\t\t\t\t<name><![CDATA[VOCALOID2 Compatible Style]]></name>
\t\t\t\t<version><![CDATA[3.0.0.1]]></version>
\t\t\t</sPlug>
\t\t\t<pStyle>
\t\t\t\t<v id="accent">50</v>
\t\t\t\t<v id="bendDep">8</v>
\t\t\t\t<v id="bendLen">0</v>
\t\t\t\t<v id="decay">50</v>
\t\t\t\t<v id="fallPort">0</v>
\t\t\t\t<v id="opening">127</v>
\t\t\t\t<v id="risePort">0</v>
\t\t\t</pStyle>
\t\t\t<singer>
\t\t\t\t<t>0</t>
\t\t\t\t<bs>0</bs>
\t\t\t\t<pc>52</pc>
\t\t\t</singer>
\t\t\t<plane>0</plane>
\t\t</vsPart>
\t</vsTrack>
\t<monoTrack>
\t</monoTrack>
\t<stTrack>
\t</stTrack>
\t<aux>
\t\t<id><![CDATA[AUX_VST_HOST_CHUNK_INFO]]></id>
\t\t<content><![CDATA[VlNDSwAAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=]]></content>
\t</aux>
</vsq4>
'''


# ----------------------------------------------------------------------
# 1. SVP 读取
# ----------------------------------------------------------------------

def load_svp(path: str) -> dict:
    """SVP 本质是 JSON，但文件末尾通常带一个 \\x00 结尾符，需要先去掉再解析。"""
    raw = open(path, "rb").read().rstrip(b"\x00")
    return json.loads(raw.decode("utf-8"))


# ----------------------------------------------------------------------
# 2. blick <-> tick 换算（按 tempo 分段累计，支持多段 tempo）
# ----------------------------------------------------------------------

@dataclass
class TempoSegment:
    start_blick: int
    end_blick: Optional[int]      # None 表示到结尾
    bpm: float
    cum_tick_at_start: float      # 该段起点对应的累计 tick 数


def build_tempo_segments(svp_tempo_list: List[dict], resolution: int) -> List[TempoSegment]:
    segs_raw = sorted(svp_tempo_list, key=lambda x: x["position"])
    if not segs_raw:
        segs_raw = [{"position": 0, "bpm": 120.0}]
    segments = []
    cum_tick = 0.0
    for i, seg in enumerate(segs_raw):
        start_blick = seg["position"]
        bpm = seg["bpm"]
        end_blick = segs_raw[i + 1]["position"] if i + 1 < len(segs_raw) else None
        segments.append(TempoSegment(start_blick, end_blick, bpm, cum_tick))
        if end_blick is not None:
            delta_blick = end_blick - start_blick
            cum_tick += delta_blick * resolution * bpm / (60 * BLICKS_PER_SECOND)
    return segments


def blick_to_tick(blick: float, segments: List[TempoSegment], resolution: int) -> float:
    seg = segments[0]
    for s in segments:
        if blick >= s.start_blick:
            seg = s
        else:
            break
    delta = blick - seg.start_blick
    return seg.cum_tick_at_start + delta * resolution * seg.bpm / (60 * BLICKS_PER_SECOND)


# ----------------------------------------------------------------------
# 3. 从 SVP 中提取所有 Group 实例（mainRef 内嵌组 + groups[] 引用组）
# ----------------------------------------------------------------------

@dataclass
class SvpNote:
    onset: int
    duration: int
    pitch: int
    lyrics: str
    phonemes: str


@dataclass
class SvpGroupInstance:
    label: str              # 仅用于日志 / Part 命名
    begin_blick: int        # blickAbsoluteBegin
    pitch_offset: float
    notes: List[SvpNote]
    pitch_points: List[tuple]  # [(blick_相对组起点, cents), ...]，来自 parameters.pitchDelta.points


def _extract_pitch_points(group_data: dict) -> List[tuple]:
    """把 SVP group.parameters.pitchDelta.points（[pos1,val1,pos2,val2,...] 的
    扁平数组，pos 是相对组起点的 blick，val 是 cents）拆成 (pos, val) 元组列表。"""
    pitch_delta = (group_data.get("parameters") or {}).get("pitchDelta") or {}
    flat = pitch_delta.get("points") or []
    points = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
    points.sort(key=lambda p: p[0])
    return points


def collect_group_instances(svp: dict, track_index: int = 0) -> List[SvpGroupInstance]:
    track = svp["tracks"][track_index]
    library_by_uuid = {g["uuid"]: g for g in svp.get("library", [])}
    instances: List[SvpGroupInstance] = []

    def build(ref: dict, group_data: dict, label: str):
        notes = [
            SvpNote(
                onset=n["onset"],
                duration=n["duration"],
                pitch=n["pitch"],
                lyrics=n.get("lyrics", ""),
                phonemes=n.get("phonemes", ""),
            )
            for n in group_data.get("notes", [])
            if n.get("musicalType", "singing") == "singing"  # 跳过纯节奏占位音符（如果有）
        ]
        if not notes:
            return  # 空组不用建 Part
        instances.append(SvpGroupInstance(
            label=label,
            # 旧版 SVP（比如这次给的 v113 文件）mainRef 里没有 blickAbsoluteBegin
            # 这个字段，缺省按 0 处理（即主组从时间轴 0 开始，notes[].onset 本身
            # 就是相对时间轴原点的坐标，和有这个字段时 onset 相对组起点的语义
            # 是一致的，因为组起点就是 0）。
            begin_blick=ref.get("blickAbsoluteBegin", 0) or 0,
            pitch_offset=ref.get("pitchOffset", 0) or 0,
            notes=notes,
            pitch_points=_extract_pitch_points(group_data),
        ))

    # mainRef 指向 track 自己内嵌的 group
    main_ref = track["mainRef"]
    build(main_ref, track["mainGroup"], track["mainGroup"].get("name", "main"))

    # groups[] 引用 library 里的可复用 group
    for ref in track.get("groups", []):
        group_data = library_by_uuid.get(ref["groupID"])
        if group_data is None:
            print(f"[警告] 找不到 groupID={ref['groupID']} 对应的 library 数据，已跳过", file=sys.stderr)
            continue
        build(ref, group_data, group_data.get("name") or ref["groupID"][:8])

    instances.sort(key=lambda g: g.begin_blick)
    return instances


def warn_on_overlaps(instances: List[SvpGroupInstance]) -> None:
    """VOCALOID 同一 vsTrack 下的 vsPart 不允许重叠，这里只做提醒，不中断转换。"""
    for a, b in zip(instances, instances[1:]):
        a_end = a.begin_blick + max(n.onset + n.duration for n in a.notes)
        if a_end > b.begin_blick:
            print(
                f"[警告] 组 \"{a.label}\" 与 \"{b.label}\" 时间上重叠"
                f"（{a_end} > {b.begin_blick} blick），VOCALOID 同一音轨不支持"
                f"重叠的 Part，如果目标编辑器严格校验，打开时可能报错，"
                f"建议人工拆到不同音轨或调整位置。",
                file=sys.stderr,
            )


# ----------------------------------------------------------------------
# 3.5 音高曲线：SVP pitchDelta（音分）-> VSQX <cc><v id="P"> 弯音曲线
# ----------------------------------------------------------------------
#
# VOCALOID4 (vsq4) 里弯音是靠两个 <cc> 控制器叠加出来的：
#   <v id="S">  PBS，Pitch Bend Sensitivity，弯音灵敏度，单位"半音"，
#               决定 <v id="P"> 的满量程对应多少音高偏移。
#   <v id="P">  PIT，实际弯音值，14bit 有符号整数，范围 -8192~8191，
#               满量程（±8192）对应 ±PBS 个半音。
# 换算关系：
#   半音偏移 = P / 8192 * PBS   =>   P = round(音分/100 / PBS * 8192)
#
# 按你的要求，PBS 固定写死成 12（即 ±12 半音 = ±1 个八度的满量程），每个
# vsPart 开头都会先写一条 <cc><t>0</t><v id="S">12</v></cc>，后面再跟
# SVP pitchDelta 曲线换算出来的一串 <v id="P"> 点。
#
# SVP 的 pitchDelta.points 是 [pos1,cents1,pos2,cents2,...] 的扁平数组，
# pos 是相对 Group 起点的 blick（和 note.onset 同一套坐标系），已经是按
# 蛮密的间隔采样过的曲线（不是稀疏关键帧），所以这里按 1:1 直接映射成
# <cc> 点，不再另外做插值/重采样。

PBS_FIXED = 12          # 固定 PBS（半音）
PIT_MIN = -8192
PIT_MAX = 8191


def cents_to_pit(cents: float, pbs: int = PBS_FIXED) -> int:
    """音分 -> PIT 原始整数值（还没裁剪范围）。"""
    return round(cents / 100.0 * 8192 / pbs)


# ------------------------------------------------------------------
# SVP 的 pitchDelta.mode 通常是 "cubic"：points 存的只是稀疏的关键控制点
# （相邻点间隔从十几毫秒到一百多毫秒不等），SV 编辑器里看到的圆滑曲线是这些
# 控制点做三次插值后的结果。VOCALOID 对相邻 <cc> 点只做线性连接，如果把控制点
# 原样 1:1 搬过去，稀疏点之间会连成直线，曲线会变成折线/锯齿状。
# 这里用 Catmull-Rom 风格的有限差分切线（支持点间距不均匀）做三次 Hermite
# 插值重建曲线，再按固定 tick 间隔重新密集采样，还原出圆滑的曲线形状。
# ------------------------------------------------------------------

def _hermite_tangents(pts: List[tuple]) -> List[float]:
    """对每个点算切线：内部点用前后相邻点的差分（Catmull-Rom 风格，允许
    点间距不均匀），端点用单侧差分。"""
    n = len(pts)
    tangents = [0.0] * n
    for i in range(n):
        if i == 0:
            t0, v0 = pts[0]
            t1, v1 = pts[1]
            tangents[i] = (v1 - v0) / (t1 - t0) if t1 != t0 else 0.0
        elif i == n - 1:
            t0, v0 = pts[i - 1]
            t1, v1 = pts[i]
            tangents[i] = (v1 - v0) / (t1 - t0) if t1 != t0 else 0.0
        else:
            tm1, vm1 = pts[i - 1]
            tp1, vp1 = pts[i + 1]
            tangents[i] = (vp1 - vm1) / (tp1 - tm1) if tp1 != tm1 else 0.0
    return tangents


def _hermite_eval(pts: List[tuple], tangents: List[float], seg: int, t: float) -> float:
    t0, v0 = pts[seg]
    t1, v1 = pts[seg + 1]
    m0, m1 = tangents[seg], tangents[seg + 1]
    h = t1 - t0
    if h <= 0:
        return v0
    s = (t - t0) / h
    s2, s3 = s * s, s * s * s
    h00 = 2 * s3 - 3 * s2 + 1
    h10 = s3 - 2 * s2 + s
    h01 = -2 * s3 + 3 * s2
    h11 = s3 - s2
    return h00 * v0 + h10 * h * m0 + h01 * v1 + h11 * h * m1


def resample_pitch_curve(points: List[tuple], sample_interval_ticks: int) -> List[tuple]:
    """points 是 [(tick, cents), ...]（可以不均匀分布），返回按固定 tick 间隔
    重采样、经过三次 Hermite 插值圆滑化后的 [(tick, cents), ...]。
    点数 < 3 时没法/没必要做样条，原样返回。"""
    pts = sorted({(round(t, 6), v) for t, v in points}, key=lambda p: p[0])
    if len(pts) < 3:
        return pts

    tangents = _hermite_tangents(pts)
    t_start, t_end = pts[0][0], pts[-1][0]
    sample_interval_ticks = max(1, sample_interval_ticks)

    result = []
    seg = 0
    t = t_start
    while t < t_end:
        while seg < len(pts) - 2 and t > pts[seg + 1][0]:
            seg += 1
        result.append((t, _hermite_eval(pts, tangents, seg, t)))
        t += sample_interval_ticks
    result.append((t_end, pts[-1][1]))  # 保证曲线终点被精确采样到
    return result


def _simplify_flat_runs(points: List[tuple]) -> List[tuple]:
    """去掉"值和左右邻居都相同"的中间点，减小文件体积，不影响曲线形状。"""
    if len(points) <= 2:
        return points
    result = [points[0]]
    for i in range(1, len(points) - 1):
        prev_v, cur_v, next_v = result[-1][1], points[i][1], points[i + 1][1]
        if cur_v == prev_v and cur_v == next_v:
            continue
        result.append(points[i])
    result.append(points[-1])
    return result


def build_pitch_cc_xml(
    inst: "SvpGroupInstance",
    segments: List[TempoSegment],
    resolution: int,
    begin_tick_song_relative: float,
    indent: str = "\t\t\t",
) -> str:
    lines = [f'{indent}<cc><t>0</t><v id="S">{PBS_FIXED}</v></cc>\n']

    if not inst.pitch_points:
        return "".join(lines)

    # 先把 (blick 相对组起点, cents) 转成 (tick 相对 Part 起点, cents)，
    # 此时先不转 PIT、不四舍五入 tick，留给插值用浮点精度。
    tick_cents_points = []
    for offset_blick, cents in inst.pitch_points:
        abs_tick = blick_to_tick(inst.begin_blick + offset_blick, segments, resolution)
        tick_cents_points.append((abs_tick - begin_tick_song_relative, cents))

    # 重采样间隔：以 480 分辨率为基准大约每 5 tick 采一次（约几毫秒级别），
    # 按模板实际 resolution 等比例换算，保证不同 resolution 下曲线密度一致。
    sample_interval = max(1, round(resolution / 96))
    smoothed = resample_pitch_curve(tick_cents_points, sample_interval)

    raw_entries = []
    clipped_count = 0
    for t, cents in smoothed:
        val = cents_to_pit(cents)
        if val > PIT_MAX or val < PIT_MIN:
            clipped_count += 1
            val = max(PIT_MIN, min(PIT_MAX, val))
        raw_entries.append((round(t), val))

    # 四舍五入到整数 tick 后，极少数相邻点可能撞在同一个 tick 上，
    # 这种情况只保留时间上更靠后的那个值，避免同一个 <t> 出现两条 <cc>。
    deduped: List[tuple] = []
    for t, v in raw_entries:
        if deduped and deduped[-1][0] == t:
            deduped[-1] = (t, v)
        else:
            deduped.append((t, v))

    deduped = _simplify_flat_runs(deduped)

    for t, v in deduped:
        lines.append(f'{indent}<cc><t>{t}</t><v id="P">{v}</v></cc>\n')

    if clipped_count:
        print(
            f"[警告] 组 \"{inst.label}\" 里有 {clipped_count} 个音高点的偏移超过了 "
            f"PBS={PBS_FIXED} 半音能表示的范围（超过 ±{PBS_FIXED * 100} 音分），"
            f"已裁剪到 PIT 的 ±8192 上限，这部分会比 SVP 里听到的弯音浅一些。",
            file=sys.stderr,
        )

    return "".join(lines)


# ----------------------------------------------------------------------
# 4. VSQX 模板解析（只读取必要的头部信息 + 可复用的 sPlug/pStyle/singer 片段）
# ----------------------------------------------------------------------

@dataclass
class VsqxTemplate:
    full_text: str
    resolution: int
    pre_measure: int
    numerator: int
    denominator: int
    head_text: str          # 从文件开头到 <vsTrack ...> 开标签结束（含 tNo/name/comment）
    tail_text: str          # 从 </vsTrack> 往后到文件结尾
    splug_block: str        # 供每个新 vsPart 复用
    pstyle_block: str
    singer_block: str


def _extract(pattern: str, text: str, flags=0) -> str:
    m = re.search(pattern, text, flags)
    if not m:
        raise ValueError(f"模板文件里没找到匹配: {pattern}")
    return m.group(1)


def parse_template_text(text: str) -> VsqxTemplate:
    resolution = int(_extract(r"<resolution>(\d+)</resolution>", text))
    pre_measure = int(_extract(r"<preMeasure>(\d+)</preMeasure>", text))
    numerator = int(_extract(r"<timeSig><m>\d+</m><nu>(\d+)</nu>", text))
    denominator = int(_extract(r"<nu>\d+</nu><de>(\d+)</de>", text))

    # vsTrack 开标签（可能带属性）到第一个 vsPart 之前，作为 head
    track_open = re.search(r"<vsTrack>.*?<tNo>.*?</comment>", text, re.S)
    if not track_open:
        raise ValueError("模板文件里没找到 <vsTrack> ... </comment> 头部")
    head_text = text[: track_open.end()]

    tail_match = re.search(r"</vsTrack>.*\Z", text, re.S)
    tail_text = text[tail_match.start():]

    splug_block = _extract(r"(<sPlug>.*?</sPlug>)", text, re.S)
    pstyle_block = _extract(r"(<pStyle>.*?</pStyle>)", text, re.S)
    singer_block = _extract(r"(<singer>.*?</singer>)", text, re.S)

    # 简单校验：如果模板里有不止一条 meter/tempo 我们目前只按第一条处理
    return VsqxTemplate(
        full_text=text,
        resolution=resolution,
        pre_measure=pre_measure,
        numerator=numerator,
        denominator=denominator,
        head_text=head_text,
        tail_text=tail_text,
        splug_block=splug_block,
        pstyle_block=pstyle_block,
        singer_block=singer_block,
    )


def parse_template(path: str) -> VsqxTemplate:
    text = open(path, "r", encoding="utf-8").read()
    return parse_template_text(text)


# ----------------------------------------------------------------------
# 5. XML 文本转义 / CDATA 辅助
# ----------------------------------------------------------------------

def cdata(text: str) -> str:
    text = text or ""
    # 极端情况下歌词本身包含 "]]>"，需要拆开，避免破坏 CDATA 结构
    text = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


# ----------------------------------------------------------------------
# 6. 生成 masterTrack 的 tempo（用 SVP 自己的 tempo 曲线，保证和用来算 tick
#    的 tempo 曲线自洽，不受模板原来拍速的影响）
# ----------------------------------------------------------------------

def build_tempo_xml(svp_tempo_list: List[dict], segments: List[TempoSegment], resolution: int) -> str:
    lines = []
    for seg in segments:
        tick = round(blick_to_tick(seg.start_blick, segments, resolution))
        v = round(seg.bpm * 100)
        lines.append(f"<tempo><t>{tick}</t><v>{v}</v></tempo>")
    return "".join(lines)


# ----------------------------------------------------------------------
# 7. 生成 note / vsPart
# ----------------------------------------------------------------------

def build_note_xml(note: SvpNote, pos_tick: int, dur_tick: int, pitch: int, indent: str = "\t\t\t") -> str:
    lyric = note.lyrics or ""
    if note.phonemes:
        # SVP 给了明确的音素（比如你现有工程里 ARPABET -> VOCALOID4 那套的输出），
        # 原样写入并锁定发音，避免引擎按字典重新猜发音。
        p_tag = f'<p lock="1">{cdata(note.phonemes)}</p>'
    else:
        # 没给音素就交给引擎按歌词自动生成发音（和 SV 里留空 phonemes 的语义一致）。
        p_tag = f"<p>{cdata(lyric)}</p>"

    i2 = indent + "\t"
    return (
        f"{indent}<note>\n"
        f"{i2}<t>{pos_tick}</t>\n"
        f"{i2}<dur>{dur_tick}</dur>\n"
        f"{i2}<n>{pitch}</n>\n"
        f"{i2}<v>64</v>\n"
        f"{i2}<y>{cdata(lyric)}</y>\n"
        f"{i2}{p_tag}\n"
        f"{i2}<nStyle>\n"
        f'{i2}\t<v id="accent">50</v><v id="bendDep">0</v><v id="bendLen">0</v>\n'
        f'{i2}\t<v id="decay">50</v><v id="fallPort">0</v><v id="opening">127</v>\n'
        f'{i2}\t<v id="risePort">0</v><v id="vibLen">0</v><v id="vibType">0</v>\n'
        f"{i2}</nStyle>\n"
        f"{indent}</note>\n"
    )


def build_vspart_xml(
    inst: SvpGroupInstance,
    index: int,
    segments: List[TempoSegment],
    resolution: int,
    pre_measure_ticks: int,
    tmpl: VsqxTemplate,
) -> str:
    begin_tick_song_relative = blick_to_tick(inst.begin_blick, segments, resolution)
    part_tick = round(pre_measure_ticks + begin_tick_song_relative)

    note_blocks = []
    last_end_tick_rel = 0
    for n in inst.notes:
        pos_abs = blick_to_tick(inst.begin_blick + n.onset, segments, resolution)
        end_abs = blick_to_tick(inst.begin_blick + n.onset + n.duration, segments, resolution)
        pos_rel = round(pos_abs - begin_tick_song_relative)
        end_rel = round(end_abs - begin_tick_song_relative)
        dur_rel = end_rel - pos_rel
        pitch = round(n.pitch + inst.pitch_offset)
        note_blocks.append(build_note_xml(n, pos_rel, dur_rel, pitch))
        last_end_tick_rel = max(last_end_tick_rel, end_rel)

    safe_label = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", inst.label)[:32]
    part_name = f"G{index}_{safe_label}" if safe_label else f"G{index}"

    pitch_cc_xml = build_pitch_cc_xml(inst, segments, resolution, begin_tick_song_relative)

    return (
        "\t\t<vsPart>\n"
        f"\t\t\t<t>{part_tick}</t>\n"
        f"\t\t\t<playTime>{last_end_tick_rel}</playTime>\n"
        f"\t\t\t<name>{cdata(part_name)}</name>\n"
        f"\t\t\t<comment>{cdata('Converted from SVP group: ' + inst.label)}</comment>\n"
        f"\t\t\t{tmpl.splug_block}\n"
        f"\t\t\t{tmpl.pstyle_block}\n"
        f"\t\t\t{tmpl.singer_block}\n"
        # vsq4 schema 里 vsPart 的子元素顺序是固定的：
        # t/playTime/name/comment/sPlug/pStyle/singer -> cc*（弯音）-> note*（音符）-> plane
        # cc 必须排在 note 之前，顺序反了 VOCALOID4 会直接报 "Invalid VSQ4 file"
        # （XML 语法本身合法，但不满足 XSD 顺序，Python 的 ElementTree 校验不出这个问题）。
        f"{pitch_cc_xml}"
        f"{''.join(note_blocks)}"
        "\t\t\t<plane>0</plane>\n"
        "\t\t</vsPart>\n"
    )


# ----------------------------------------------------------------------
# 8. 整合
# ----------------------------------------------------------------------

def convert(svp_path: str, template_path: Optional[str], output_path: str, track_index: int = 0) -> None:
    svp = load_svp(svp_path)
    if template_path:
        tmpl = parse_template(template_path)
    else:
        print("[提示] 未指定 --template，使用内置的默认空白模板"
              "（resolution=480, preMeasure=4, 4/4 拍, MIKU_V4X_Original 音色）。",
              file=sys.stderr)
        tmpl = parse_template_text(DEFAULT_TEMPLATE_TEXT)

    resolution = tmpl.resolution
    ticks_per_measure = resolution * 4 * tmpl.numerator // tmpl.denominator
    pre_measure_ticks = tmpl.pre_measure * ticks_per_measure

    meters = svp.get("time", {}).get("meter", [])
    if len(meters) > 1:
        print(f"[警告] SVP 里有 {len(meters)} 条拍号变化，本脚本只按第一条处理，"
              f"其余会被忽略。", file=sys.stderr)

    tempo_list = svp.get("time", {}).get("tempo", [{"position": 0, "bpm": 120.0}])
    segments = build_tempo_segments(tempo_list, resolution)

    instances = collect_group_instances(svp, track_index=track_index)
    if not instances:
        raise ValueError("SVP 里没有找到任何带音符的 Group，没有可转换的内容")
    warn_on_overlaps(instances)

    tempo_xml = build_tempo_xml(tempo_list, segments, resolution)
    # 用 SVP 自己的 tempo 替换模板里 masterTrack 的 tempo，保持 tick 网格自洽
    head_with_tempo = re.sub(r"(<tempo>.*?</tempo>)+", tempo_xml, tmpl.head_text, count=1, flags=re.S)

    parts_xml = "".join(
        build_vspart_xml(inst, i, segments, resolution, pre_measure_ticks, tmpl)
        for i, inst in enumerate(instances)
    )

    output_text = head_with_tempo + "\n" + parts_xml + tmpl.tail_text

    with open(output_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(output_text)

    print(f"转换完成：{len(instances)} 个 Group -> {len(instances)} 个 vsPart", file=sys.stderr)
    for i, inst in enumerate(instances):
        begin_tick = round(pre_measure_ticks + blick_to_tick(inst.begin_blick, segments, resolution))
        print(f"  Group[{i}] \"{inst.label}\": {len(inst.notes)} 个音符, "
              f"blickAbsoluteBegin={inst.begin_blick} -> Part.<t>={begin_tick}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="SVP -> VSQX 转换（保留 Group 原始位置）")
    ap.add_argument("svp", help="输入的 .svp 文件")
    ap.add_argument("--template", default=None,
                     help="作为结构/音色模板的 .vsqx 文件（可选，不填则使用内置的默认空白模板）")
    ap.add_argument("-o", "--output", required=True, help="输出的 .vsqx 文件路径")
    ap.add_argument("--track", type=int, default=0, help="转换 SVP 里的第几条音轨，默认第 0 条")
    args = ap.parse_args()
    convert(args.svp, args.template, args.output, track_index=args.track)


if __name__ == "__main__":
    main()
