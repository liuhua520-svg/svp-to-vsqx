# svp_to_vsqx.py

将 Synthesizer V Studio 工程文件（`.svp`）转换为 VOCALOID4 工程文件（`.vsqx`），
保留音符位置、歌词/音素，并将 SVP 的音高曲线（pitchDelta）转换为 VSQX 的
弯音控制器（PBS 固定为 12 半音）。

## 依赖

- Python 3.7+（标准库即可，无需额外安装任何第三方包）

## 用法

```bash
python svp_to_vsqx.py <输入.svp> -o <输出.vsqx>
python svp_to_vsqx.py <输入.svp> --template <模板.vsqx> -o <输出.vsqx>
```

### 示例

```bash
# 不提供模板，使用脚本内置的默认空白模板
python svp_to_vsqx.py SAMPLE12345.svp -o output.vsqx

# 提供自己的模板文件（用来带上想要的音色/演唱风格配置）
python svp_to_vsqx.py SAMPLE12345.svp --template SAMPLE12345.vsqx -o output.vsqx
```

### 参数说明

| 参数 | 是否必填 | 说明 |
|---|---|---|
| `svp` | 是（位置参数） | 输入的 `.svp` 文件路径 |
| `--template` | 否 | 一个已有的 `.vsqx` 文件，用来提供 SVP 里没有的信息（见下方"模板的作用"）。不提供时使用脚本内置的默认空白模板（`resolution=480`、`preMeasure=4`、`4/4` 拍、`MIKU_V4X_Original` 音色） |
| `-o, --output` | 是 | 转换结果输出到哪个 `.vsqx` 文件 |
| `--track` | 否，默认 `0` | 转换 SVP 里的第几条音轨（从 0 开始计数），SVP 有多条音轨时用这个指定 |

## 模板的作用

VSQX 需要一些 SVP 文件里没有的信息，比如：

- `resolution`（每四分音符多少 tick）
- `preMeasure`（开头空白小节数）
- 音源 / 音色相关配置（`sPlug`、`pStyle`、`singer`）

脚本内置了一个干净的默认模板（结构合法、能在 VOCALOID 编辑器里正常打开，
但不含任何真实音符），不提供 `--template` 时会自动使用它，所以大多数情况
下不需要额外准备模板文件就能直接转换。

如果想要指定特定音色 / 演唱风格，可以自己提供一个**结构完整、能在
VOCALOID 编辑器里正常打开**的 `.vsqx` 文件作为模板。转换时脚本只会替换
其中的：

- `masterTrack` 的 tempo/拍号 → 换成 SVP 自己的 tempo/拍号，保证时间换算自洽
- `vsTrack` 下的 `vsPart`（音符、歌词、弯音曲线）→ 换成 SVP 转换出来的内容

模板里其他部分（`vender`、`version`、`vVoiceTable`、`mixer`、`monoTrack`、
`stTrack`、`aux` 等）原样保留，不受影响。

## 转换逻辑简述

- **时间轴**：SVP 的时间单位是 blick（`1 秒 = 1,411,200,000 blick`，与 tempo 无关），
  按 SVP 自身的多段 tempo 曲线累计换算成 VSQX 的 tick；`preMeasure` 造成的整体偏移
  会自动加上。

  > 这个常数网上常见资料写的是 705,600,000（早期版本的脚本也用过这个值），
  > 但实测比对真实文件后确认是错的：用 705,600,000 算出来的时间刚好是
  > 实际时间的 2 倍，会导致 VSQX 里的音符位置相对 SVP 原始小节位置整体
  > 拉伸一倍。现在已经修正为正确值的 1,411,200,000。
- **音符结构**：SVP 的每个 Group（`mainRef` 内嵌组，或 `groups[]` 引用 `library[]`
  的可复用组）→ 一个 VSQX `vsPart`，不拆散、不合并，保留原始的"容器 + 相对偏移"两层结构。
- **音高曲线**：SVP `parameters.pitchDelta.points`（音分，相对组起点的 blick 位置）
  → VSQX `<cc><v id="P">` 弯音点，换算公式：

  ```
  P = round(音分 / 100 / PBS * 8192)      其中 PBS 固定为 12
  ```

  每个 `vsPart` 开头会先写一条 `<cc><t>0</t><v id="S">12</v></cc>` 设定 PBS。
  超出 ±12 半音（±1200 音分）范围的点会被裁剪到 PIT 允许的 ±8192，并在终端打印警告。
- **音素**：SVP `note.phonemes` 非空时原样写入并加 `lock="1"`（避免引擎按字典重新猜
  发音）；为空时只写歌词，交给引擎自动生成发音。

## 已知限制

- 拍号变化：SVP 里如果有多条拍号记录，只取第一条，其余会打印警告并忽略。
- Part 重叠：如果转换出的多个 `vsPart` 时间上重叠，只会打印警告提醒，不会自动
  拆分到不同音轨（VOCALOID 同一音轨下的 Part 一般不允许重叠，如遇报错请手动
  挪到不同音轨）。
- 音素映射：不做 ARPABET → VOCALOID4 音素体系的自动映射，只做透传。

## 常见问题排查

**Q: 报错提示找不到 `<resolution>` / `<preMeasure>` 等标签？**
A: `--template` 指定的文件不是合法的 vsq4（VOCALOID4）格式，请换一个能在
VOCALOID4 编辑器里正常打开的 `.vsqx` 文件。

**Q: 音高听起来被削平了/终端提示"已裁剪到 PIT 的上限"？**
A: 该处的音高偏移超过了 PBS=12 半音能表示的范围（超过 ±1200 音分），当前版本会硬
裁剪。如果原曲有超过一个八度的滑音，转换后这部分会比原始效果浅。

**Q: 转换后打开文件提示格式错误？**
A: 确认输出路径以 `.vsqx` 结尾，并且原始模板本身能正常打开；也可以用
`python -c "import xml.etree.ElementTree as ET; ET.parse('output.vsqx')"`
快速检查生成文件的 XML 是否合法。
