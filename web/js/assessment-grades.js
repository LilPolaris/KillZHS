// Only the refreshed platform list is authoritative for published grades.
const valid = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;

export function gradeLabel(row) {
  if (row.state === 3) return "待批阅";
  if (row.state !== 4) return "尚未出分";
  if (valid(row.score)) {
    const total = valid(row.total_score) && row.total_score > 0 ? ` / ${row.total_score}` : "";
    return `${row.score}${total} 分`;
  }
  if (valid(row.score_percent) && row.score_percent <= 100) return `百分制 ${row.score_percent} 分`;
  return "成绩未公布";
}

export function assessmentStatus(row, result) {
  if (row.state === 3) return "已提交，待批阅";
  if (row.state === 4) return "已提交";
  return result?.message || ([1, 2, 5].includes(row.state) ? "待作答" : "不可作答");
}
