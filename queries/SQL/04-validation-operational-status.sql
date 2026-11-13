-- Operational validation states, including unsuccessful executions.
-- Parameters: :catalogue_id and :category_id (NULL disables each filter).
SELECT
    v.execution_status,
    v.http_status,
    v.error,
    COUNT(*) AS count
FROM validations AS v
JOIN datasets AS d ON d.dataset_id = v.dataset_id
WHERE (:catalogue_id IS NULL OR d.catalogue_id = :catalogue_id)
  AND (:category_id IS NULL OR d.category_id = :category_id)
GROUP BY v.execution_status, v.http_status, v.error
ORDER BY v.execution_status, v.http_status;
