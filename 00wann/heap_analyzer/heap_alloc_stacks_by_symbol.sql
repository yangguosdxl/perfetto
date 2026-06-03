-- 查询调用栈中包含指定符号的 heap profile 分配栈。
-- 使用方式：
--   trace_processor query -f heap_analyzer/heap_alloc_stacks_by_symbol.sql /path/to/symbolized-trace
--
-- 修改下面 target.symbol 即可切换目标函数。GLOB 是子串匹配，适合匹配带参数或修饰后的符号名。
WITH RECURSIVE
target(symbol) AS (
  VALUES('il2cpp::vm::Class::Init')
),
target_node(callsite_id) AS (
  -- Native heap profile UI 展示的是 stack_profile_symbol 里的符号化名称；
  -- stack_profile_frame.name 可能为空或仍是原始地址/未反混淆名称。
  SELECT cs.id
  FROM stack_profile_callsite AS cs
  JOIN stack_profile_frame AS f ON f.id = cs.frame_id
  JOIN target AS t
  WHERE IFNULL(f.deobfuscated_name, '') GLOB '*' || t.symbol || '*'
     OR IFNULL(f.name, '') GLOB '*' || t.symbol || '*'
     OR EXISTS (
       SELECT 1
       FROM stack_profile_symbol AS s
       WHERE s.symbol_set_id = f.symbol_set_id
         AND s.name GLOB '*' || t.symbol || '*'
     )
),
matched_callsite(leaf_callsite_id) AS (
  -- 目标节点自身以及所有子节点都是“调用栈包含目标符号”的分配位置。
  SELECT callsite_id
  FROM target_node

  UNION ALL

  SELECT child.id
  FROM matched_callsite AS parent
  JOIN stack_profile_callsite AS child ON child.parent_id = parent.leaf_callsite_id
),
alloc_group AS (
  -- heap_profile_allocation.size/count 可能为正也可能为负，这里按 callsite 汇总净变化。
  SELECT
    h.upid,
    h.heap_name,
    h.callsite_id,
    SUM(h.count) AS net_alloc_count,
    SUM(h.size) AS net_alloc_bytes
  FROM heap_profile_allocation AS h
  JOIN (SELECT DISTINCT leaf_callsite_id FROM matched_callsite) AS m
    ON m.leaf_callsite_id = h.callsite_id
  GROUP BY h.upid, h.heap_name, h.callsite_id
),
stack_walk(leaf_callsite_id, callsite_id, depth, frame_name, parent_id) AS (
  -- 只对已命中的分配 callsite 展开完整调用栈。
  SELECT
    ag.callsite_id AS leaf_callsite_id,
    cs.id AS callsite_id,
    cs.depth,
    COALESCE(
      (
        SELECT GROUP_CONCAT(symbol_ordered.name, char(10) || '     [inline] ')
        FROM (
          SELECT s.name
          FROM stack_profile_symbol AS s
          WHERE s.symbol_set_id = f.symbol_set_id
          ORDER BY s.id
        ) AS symbol_ordered
      ),
      NULLIF(f.deobfuscated_name, ''),
      NULLIF(f.name, ''),
      '[unknown]'
    ) AS frame_name,
    cs.parent_id
  FROM alloc_group AS ag
  JOIN stack_profile_callsite AS cs ON cs.id = ag.callsite_id
  JOIN stack_profile_frame AS f ON f.id = cs.frame_id

  UNION ALL

  -- 沿 parent_id 向根节点展开，得到一次分配对应的完整调用栈。
  SELECT
    sw.leaf_callsite_id,
    parent.id AS callsite_id,
    parent.depth,
    COALESCE(
      (
        SELECT GROUP_CONCAT(symbol_ordered.name, char(10) || '     [inline] ')
        FROM (
          SELECT s.name
          FROM stack_profile_symbol AS s
          WHERE s.symbol_set_id = pf.symbol_set_id
          ORDER BY s.id
        ) AS symbol_ordered
      ),
      NULLIF(pf.deobfuscated_name, ''),
      NULLIF(pf.name, ''),
      '[unknown]'
    ) AS frame_name,
    parent.parent_id
  FROM stack_walk AS sw
  JOIN stack_profile_callsite AS parent ON parent.id = sw.parent_id
  JOIN stack_profile_frame AS pf ON pf.id = parent.frame_id
)
SELECT
  ag.upid,
  p.pid,
  p.name AS process_name,
  ag.heap_name,
  ag.callsite_id,
  ag.net_alloc_count,
  ag.net_alloc_bytes,
  ROUND(ag.net_alloc_bytes / 1048576.0, 3) AS net_alloc_mib,
  (
    SELECT GROUP_CONCAT(ordered.frame_name, char(10) || '  <- ')
    FROM (
      SELECT sw.frame_name
      FROM stack_walk AS sw
      WHERE sw.leaf_callsite_id = ag.callsite_id
      ORDER BY sw.depth DESC
    ) AS ordered
  ) AS stack
FROM alloc_group AS ag
LEFT JOIN process AS p USING (upid)
ORDER BY ABS(ag.net_alloc_bytes) DESC
LIMIT 200;
