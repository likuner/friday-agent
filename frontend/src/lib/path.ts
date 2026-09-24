// 工作区路径的展示工具：列表里只给最末级目录名（全路径由调用方挂 title 悬停显示）
export function shortDir(root: string): string {
  const parts = root.split('/').filter(Boolean);
  return parts[parts.length - 1] ?? root;
}
