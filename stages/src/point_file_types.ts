// 点表文件结构类型（doc_parsers 解析产物 / point_pipeline 两遍法输入）。
// M1 起定义收口到共享包 @c4/stages（c4_asset_learning.md §2.2：识别流水线
// 训练端与运行端同构）——agent 的 doc_parsers 自此处转出，依赖方向保持
// agent → stages 单向。

/** 单个逻辑块（sheet / csv / 文本段）的解析产物 */
export interface ParsedPointBlock {
    /** 逻辑块标题：sheet 名 / "csv" / "text" */
    title: string;
    kind: "table" | "text";
    /** 表头行（无表头为 null——首行即数据） */
    header: string[] | null;
    /** 非数据行（段标题等）：row = 该行之后首个数据行的行号（1 起） */
    nonData: Array<{ row: number; text: string }>;
    /** 数据行（保序；单元格为存储值字符串化，空单元格为 ""） */
    rows: string[][];
}

/** 整份点表文件的多块解析产物 */
export interface ParsedPointFile {
    /** 原始文件名（上传原名，语境行与同名替换判定用） */
    name: string;
    blocks: ParsedPointBlock[];
}
