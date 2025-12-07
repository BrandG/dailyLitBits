docker exec mongo mongosh slow_reader_db --quiet --eval "db.chunks.countDocuments({ recap: { \$ne: null } })"
