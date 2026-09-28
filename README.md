<img src="docs/icon.png" width="96" align="right" alt="icon">

# CT Low-Dose Simulator

[![tests](https://github.com/kawawwww/CT_lowdose_simulator/actions/workflows/tests.yml/badge.svg)](https://github.com/kawawwww/CT_lowdose_simulator/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

通常線量で撮影された CT 画像（DICOM）から、**低線量で撮影した場合の CT 画像を模擬する**デスクトップアプリです。
投影データ上で光子数に応じたノイズを付加するため、画像上でガウシアンノイズを足すよりも現実に近いノイズ（被写体の厚みに依存する強度・筋状のパターン）が得られます。

*English: A desktop app that simulates reduced-dose CT images from routine-dose DICOM series by projection-domain noise insertion. The target dose is set relative to the mAs recorded in the DICOM header, and the noise level is calibrated against the noise measured in the original image.*

![screenshot](docs/screenshot.png)

## 特長

- **DICOM の撮影条件を基準に線量を指定** — `Exposure` / `XRayTubeCurrent × ExposureTime` などから元画像の mAs を読み取り、「1/2 線量」「20 mAs」のように指定できます。スライスごとに読むため管電流変調にも対応します。
- **差分ノイズの付加** — 元画像がすでに持っているノイズを考慮し、目標線量との差の分だけノイズを付加します。
- **ノイズ量の校正** — 元画像の均一な領域（ROI）のノイズ SD から、シミュレーションの光子数を自動で決めます。
- **解像度・CT 値を保持** — ノイズ成分だけを再構成して元画像に加算するため、画像がぼけたり CT 値がずれたりしません。
- **GPU / CPU** — NVIDIA GPU があれば CUDA で高速に、なければ CPU で動作します。
- **DICOM 出力** — 元のヘッダを引き継ぎ、UID の振り直し・撮影条件タグ（mA, mAs, CTDIvol）の更新を行います。処理条件は JSON で保存されます。

## インストール

Python 3.10 以上が必要です。

```bash
pip install git+https://github.com/kawawwww/CT_lowdose_simulator.git
```

開発用にソースから入れる場合:

```bash
git clone https://github.com/kawawwww/CT_lowdose_simulator.git
cd CT_lowdose_simulator
pip install -e ".[test]"
```

[ASTRA Toolbox](https://astra-toolbox.com/)（PyPI 版は CUDA ランタイム同梱）と PySide6 も自動でインストールされます。GPU を使う場合は NVIDIA ドライバが必要です。

## 使い方（GUI）

```bash
ctlowdose-gui
```

Windows では、次のコマンドでデスクトップにアイコン付きのショートカットを作れます。

```bash
python -m ctlowdose --create-shortcut
```

1. **入力フォルダ** に CT シリーズの DICOM フォルダを指定します。撮影条件（mAs, kVp, 再構成カーネルなど）が表示されます。
2. **目標線量** を「線量比」（例: 0.5 = 1/2 線量）または「目標 mAs」で指定します。
3. **ノイズ量の校正**: 画像上の肝実質・筋肉・大動脈など**均一な領域を左クリック**して ROI を置き（右クリックで削除）、「校正を実行」を押します。
4. **プレビュー** で表示中のスライスの結果を確認します。ROI 一覧に元画像と模擬画像の平均±SD が表示されます。
5. **出力フォルダ** を指定して「一括処理して保存」を押します。

## 使い方（コマンドライン）

```bash
# 1/2 線量。スライス48の2か所のROIで校正してから処理
ctlowdose INPUT_DIR OUTPUT_DIR --ratio 0.5 --calibrate-roi 48:280:115:18 --calibrate-roi 48:380:222:10

# 目標 20 mAs、校正済みの光子数を直接指定
ctlowdose INPUT_DIR OUTPUT_DIR --target-mAs 20 --photons-per-mAs 9800
```

ROI は `スライス番号:行:列:半径`（スライス番号は 0 始まり、スライス位置順）です。`ctlowdose --help` で全オプションを表示します。

## 理論的背景

処理の全体像は次のとおりです。

```
元画像(HU) → μ → 順投影(線積分 p) → 透過率 T → 光子数にノイズを付加 → 対数変換
          → ノイズ成分のみ FBP → 元画像に加算 → 低線量画像(HU)
```

### 1. CT 値と線減弱係数

CT 値は水の線減弱係数 $\mu_w$ を基準に定義されます。

```math
\mathrm{HU} = 1000 \cdot \frac{\mu - \mu_w}{\mu_w}
\quad\Longleftrightarrow\quad
\mu = \mu_w \left( 1 + \frac{\mathrm{HU}}{1000} \right)
```

$\mu_w$ は既定で 0.2 cm⁻¹（実効エネルギー 60〜70 keV 付近の水）です。−1000 HU 未満は空気（$\mu = 0$）として扱います。

### 2. 投影と Beer–Lambert 則

入射光子数 $I_0$ の X 線が経路 $L$ を通過したときの期待検出光子数は

```math
\lambda = I_0 \exp\!\left( -\int_L \mu(\mathbf{x})\, d\ell \right) = I_0\, T,
\qquad
p = -\ln T = \int_L \mu\, d\ell
```

です。画像を画素サイズ $\Delta x$（`PixelSpacing`）で離散化すると、検出器ビン $j$ の線積分は

```math
p_j = \Delta x \sum_i a_{ji}\, \mu_i
```

となります（$a_{ji}$ はシステム行列。2D 平行ビーム、$0 \le \theta < \pi$ を等間隔に `num_angles` 方向）。
$\Delta x$ を掛けることで、被写体の実際の厚みに応じた透過率 $T$ が得られます。

### 3. 光子統計と投影値のノイズ

検出光子数 $N$ はポアソン分布に従い、そこに電子ノイズ $e \sim \mathcal{N}(0, \sigma_e^2)$ が加わります。

```math
N \sim \mathrm{Poisson}(\lambda) + e,
\qquad
\hat{p} = \ln \frac{I_0}{N}
```

デルタ法（$d\hat{p}/dN = -1/N$）により、対数変換後の投影値の分散は

```math
\mathrm{Var}[\hat{p}] \approx \frac{\mathrm{Var}[N]}{\lambda^2}
= \frac{\lambda + \sigma_e^2}{\lambda^2}
\approx \frac{1}{I_0\, T}
```

です（量子ノイズが支配的な場合）。**透過率 $T$ が小さい経路（体の厚い方向、骨を通る方向）ほど投影値のノイズが大きい**ことが、CT ノイズの空間的な不均一性や筋状アーチファクトの原因になります。

### 4. 線量と入射光子数

管電圧が同じなら、入射光子数は管電流時間積（mAs）に比例します。

```math
I_0 = k \cdot \mathrm{mAs}
```

$k$ はコード中の `photons_per_mAs` です。元画像の mAs（DICOM から取得）に対する目標線量の比を $a$ とすると

```math
I_{\mathrm{ref}} = k \cdot \mathrm{mAs}_{\mathrm{orig}},
\qquad
I_{\mathrm{low}} = a\, I_{\mathrm{ref}}
\qquad (0 < a \le 1)
```

### 5. 差分ノイズの付加

元画像はすでに撮影線量相当のノイズ（分散 $1/(I_{\mathrm{ref}}T)$）を含んでいます。
低線量で必要な分散は $1/(a I_{\mathrm{ref}}T)$ なので、**追加すべき分散は差分だけ**です。

```math
\Delta\mathrm{Var}[\hat{p}]
= \frac{1}{a\, I_{\mathrm{ref}} T} - \frac{1}{I_{\mathrm{ref}} T}
= \frac{1-a}{a\, I_{\mathrm{ref}} T}
```

これをカウント領域で実現するため、低線量の検出光子数を次のように生成します（$z \sim \mathcal{N}(0,1)$）。

```math
\tilde{N} = a\,\lambda_{\mathrm{ref}} + \sqrt{a(1-a)\,\lambda_{\mathrm{ref}}}\; z + e,
\qquad
\lambda_{\mathrm{ref}} = I_{\mathrm{ref}}\, T
```

平均 $a\lambda_{\mathrm{ref}}$、分散 $a(1-a)\lambda_{\mathrm{ref}}$ なので、デルタ法により

```math
\mathrm{Var}\!\left[ \ln \frac{a I_{\mathrm{ref}}}{\tilde{N}} \right]
\approx \frac{a(1-a)\,\lambda_{\mathrm{ref}}}{(a\,\lambda_{\mathrm{ref}})^2}
= \frac{1-a}{a\, I_{\mathrm{ref}} T}
```

となり、ちょうど差分の分散が得られます。元画像のノイズと独立なので、合計は $1/(aI_{\mathrm{ref}}T)$ すなわち低線量相当になります。
これは「高線量の投影データを二項間引きして低線量データを作る」考え方（Yu et al., 2012）を、カウントが十分大きいとしてガウス近似したものです。
$\tilde{N}$ が 0 以下になる極端な光子飢餓では `eps`（既定 1 カウント）で下限を設けます。

オプションの「元画像のノイズ分を差し引く」をオフにすると、元画像をノイズなしとみなし、$N \sim \mathrm{Poisson}(a I_{\mathrm{ref}} T) + e$ でフルのノイズを付加します。

### 6. 画像領域への伝播（ノイズ画像の加算）

FBP は線形演算 $\mathcal{R}^{-1}$ なので、投影値を真値とノイズに分けると再構成も分けられます。

```math
\mathcal{R}^{-1}\!\left( p + \delta p \right) = \mathcal{R}^{-1} p + \mathcal{R}^{-1} \delta p
```

そこで、**ノイズ成分 $\delta p$ だけを再構成して元画像に加算**します。

```math
\mathrm{HU}_{\mathrm{low}}(\mathbf{x})
= \mathrm{HU}_{\mathrm{orig}}(\mathbf{x})
+ \frac{1000}{\mu_w} \left[ \mathcal{R}^{-1}
\left( \frac{\hat{p}_{\mathrm{low}} - p}{\Delta x} \right) \right]\!(\mathbf{x})
```

画像全体を再投影・再構成し直す方法では、補間によって元画像がぼけ、元のノイズも減ってしまいます（例: SD 22.9 → 15.2 HU）。
加算方式ではこの劣化が起きず、元画像の解像度と CT 値がそのまま保たれます（オプションで再投影方式も選べます）。

FBP の各画素の分散は、その画素を通る投影値の分散の重み付き和 $\sigma^2(\mathbf{x}) \approx \sum_j h_j(\mathbf{x})^2\, \mathrm{Var}[\hat{p}_j]$ です。
すべての $\mathrm{Var}[\hat{p}_j]$ が $(1-a)/a$ 倍されるので、付加ノイズの分散も同じ倍率になり、元のノイズと合わせると

```math
\sigma_{\mathrm{low}}^2(\mathbf{x}) = \sigma_{\mathrm{orig}}^2(\mathbf{x}) + \frac{1-a}{a}\,\sigma_{\mathrm{orig}}^2(\mathbf{x})
= \frac{\sigma_{\mathrm{orig}}^2(\mathbf{x})}{a}
\qquad\Longrightarrow\qquad
\sigma_{\mathrm{low}} = \frac{\sigma_{\mathrm{orig}}}{\sqrt{a}}
```

が成り立ちます（1/2 線量で SD は √2 倍）。ただしこれは次節の校正で $k$ が正しく決まっている場合です。

### 7. ノイズ量の校正

本ソフトの幾何（2D 平行ビーム、検出器ピッチ、単色 X 線、Ram-Lak フィルタ）は実機と異なるため、$k$ は実機の光子数ではなく、それらの違いを吸収する**実効値**です。そこで $k$ を画像から決めます。

条件は「**元画像と同線量（$a=1$）のフルノイズを生成したとき、その画像上の SD が元画像の SD と一致する**」ことです。
ROI $m = 1,\dots,M$ での分散を平均して

```math
\sigma_{\mathrm{orig}}^2 = \frac{1}{M}\sum_{m} \mathrm{Var}_m\!\left[\mathrm{HU}_{\mathrm{orig}}\right],
\qquad
\sigma_{\mathrm{sim}}^2(k) = \frac{1}{M}\sum_{m} \mathrm{Var}_m\!\left[ \frac{1000}{\mu_w}\,\mathcal{R}^{-1} \delta p_{a=1}(k) \right]
```

とします。量子ノイズが支配的なら $\sigma_{\mathrm{sim}}^2 \propto 1/k$ なので、

```math
k_{n+1} = k_n \left( \frac{\sigma_{\mathrm{sim}}(k_n)}{\sigma_{\mathrm{orig}}} \right)^{2}
```

で更新します。電子ノイズがあると厳密には比例しないため、$|\sigma_{\mathrm{sim}}/\sigma_{\mathrm{orig}} - 1| < 2\%$ になるまで反復します（通常 2〜3 回で収束）。$\sigma_{\mathrm{sim}}$ は 4 回のノイズ実現の平均です。

**ROI を均一な領域に置く理由**: ROI 内の分散は $\mathrm{Var}_m = \sigma_{\mathrm{noise}}^2 + \sigma_{\mathrm{anatomy}}^2$ であり、血管や境界が入ると $\sigma_{\mathrm{orig}}$ が過大になります。すると $k$ が過小に推定され、付加ノイズが過大になります。
また、体内の位置によるノイズの違い（中心ほど大きい）はモデル自体が再現しますが、実機のボウタイフィルタなどはモデル化していないため、中心寄りと辺縁寄りの ROI を混ぜて平均すると偏りが減ります。

### 8. 検証例

CHAOS データセット症例 1（140 mAs, 120 kVp）で、肝実質・傍脊柱筋の 4 ROI を用いて校正した結果です。

| 線量比 $a$ | 模擬 mAs | ROI の SD（実測） | 理論値 $\sigma_{\mathrm{orig}}/\sqrt{a}$ |
|---|---|---|---|
| 1 | 140 | 22.9 HU | — |
| 0.5 | 70 | 32.8 HU | 32.4 HU |
| 0.25 | 35 | 44.3 HU | 45.9 HU |

平均 CT 値の変化は 0.03 HU でした。

## 出力

- 元ファイルと同じ名前の DICOM（int16, RescaleSlope=1, RescaleIntercept=0, Explicit VR Little Endian）
  - `SeriesInstanceUID` / `SOPInstanceUID` を新規発行
  - `XRayTubeCurrent`, `Exposure`, `CTDIvol` などを線量比に合わせて更新
  - `ImageType` を `DERIVED\SECONDARY` に、`SeriesDescription` を `Simulated low dose x0.5` などに変更
- `ctlowdose_params.json` — ソフトウェアのバージョン、計算方式、全パラメータ、スライスごとの mAs

## 制限事項

- 2D 平行ビーム・単色 X 線のモデルです。ビームハードニング、ボウタイフィルタ、ヘリカル補間、自動露出制御の挙動は模擬しません。
- ノイズの SD は校正で一致させますが、ノイズの質感（NPS）は元画像の再構成カーネルとは一致しません。
- 逐次近似再構成（AIDR, ASiR, iDose など）の画像では、元画像のノイズが光子数から期待されるより小さいため、付加するノイズが過小になります。FBP 画像での使用を推奨します。
- 詳細設定（投影数、検出器ピッチ、電子ノイズ、計算方式）を変更したら校正をやり直してください。GPU と CPU では再構成の実装が異なるため、校正値がわずかに異なります。

## 免責

本ソフトウェアは研究・教育目的のものです。**臨床診断には使用しないでください。**
患者データを扱う際は、所属機関の規程に従って匿名化・管理してください（`.gitignore` で `*.dcm` を除外しています）。

## 参考文献

- W. van Aarle et al., "Fast and flexible X-ray tomography using the ASTRA toolbox," *Optics Express* 24(22), 2016.
- L. Yu et al., "Development and validation of a practical lower-dose-simulation tool for optimizing CT scan protocols," *Journal of Computer Assisted Tomography* 36(4), 2012.

スクリーンショットの画像は [CHAOS challenge](https://chaos.grand-challenge.org/) のデータセットを使用しています。

## ライセンス

[MIT License](LICENSE)。依存ライブラリの ASTRA Toolbox は GPLv3 です。
