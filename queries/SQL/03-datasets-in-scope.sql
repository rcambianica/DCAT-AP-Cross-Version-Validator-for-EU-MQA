-- Dataset inventory rows selected for quantitative analysis.
-- Parameters: :catalogue_id and :category_id (NULL disables each filter).
SELECT
    dataset_id,
    catalogue_id,
    category_id,
    fetch_status,
    fetch_http_status,
    fetch_error
FROM datasets
WHERE (:catalogue_id IS NULL OR catalogue_id = :catalogue_id)
  AND (:category_id IS NULL OR category_id = :category_id)
ORDER BY dataset_id;
