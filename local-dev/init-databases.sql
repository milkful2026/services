-- Runs once, on first container start, as the postgres superuser.
-- Separate databases per service, mirroring each service's own Aurora
-- cluster in real deployments.
CREATE DATABASE milkful_user;
CREATE DATABASE milkful_inventory;
