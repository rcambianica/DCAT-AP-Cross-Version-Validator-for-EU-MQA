SELECT 
    shape_model,
    COUNT(*) AS total_validations,
    SUM(CASE WHEN conforms = 1 THEN 1 ELSE 0 END) AS conformant_count,
    ROUND((SUM(CASE WHEN conforms = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*)), 2) AS percentage_conforms
FROM 
    validations
WHERE execution_status = 'validated'
  AND http_status = 200
  AND conforms IS NOT NULL
  AND (error IS NULL OR TRIM(error) = '')
GROUP BY 
    shape_model;
