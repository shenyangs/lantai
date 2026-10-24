# 工具边界

澜台 Runtime 负责编排与交付；OfficeCLI 只在事务边界内写入；python-docx 用于明确的结构化操作；GenOffice 负责用户编辑。所有写入都从当前文件开始，并保留哈希与回滚证据。
