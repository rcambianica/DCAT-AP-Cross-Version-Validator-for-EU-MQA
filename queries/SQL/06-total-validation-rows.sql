-- All validation rows in the selected dataset scope (diagnostic denominator).
-- Parameters: :catalogue_id and :category_id (NULL disables each filter).
SELECT COUNT(*)
FROM validations AS v
JOIN datasets AS d ON d.dataset_id = v.dataset_id
WHERE (:catalogue_id IS NULL OR d.catalogue_id = :catalogue_id)
  AND (:category_id IS NULL OR d.category_id = :category_id);
