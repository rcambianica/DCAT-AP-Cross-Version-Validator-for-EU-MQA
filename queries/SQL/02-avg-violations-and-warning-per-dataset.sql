SELECT 
    shape_model,
    COUNT(*) AS total_validations,
    ROUND(AVG(violations), 2) AS avg_violations_per_dataset,
    ROUND(AVG(warnings), 2) AS avg_warnings_per_dataset
FROM 
    validations
WHERE execution_status = 'validated'
  AND http_status = 200
  AND conforms IS NOT NULL
  AND (error IS NULL OR TRIM(error) = '')
GROUP BY 
    shape_model;
