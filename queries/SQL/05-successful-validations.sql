-- Successful validations used as the quantitative source.
-- Parameters: :catalogue_id and :category_id (NULL disables each filter).
SELECT
    v.id,
    v.dataset_id,
    v.validator_url,
    v.shape_model,
    v.conforms,
    v.results,
    v.violations,
    v.warnings,
    v.infos,
    v.http_status,
    v.execution_status,
    v.validated_at,
    v.report_status
FROM validations AS v
JOIN datasets AS d ON d.dataset_id = v.dataset_id
WHERE (:catalogue_id IS NULL OR d.catalogue_id = :catalogue_id)
  AND (:category_id IS NULL OR d.category_id = :category_id)
  AND v.execution_status = 'validated'
  AND v.http_status = 200
  AND v.conforms IS NOT NULL
  AND (v.error IS NULL OR TRIM(v.error) = '')
ORDER BY v.shape_model, v.dataset_id, v.validator_url;
