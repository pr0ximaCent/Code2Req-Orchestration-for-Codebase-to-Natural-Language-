package io.demo.infrastructure;

@Mapper
public interface ArticleReadService {
  List<Article> findFeed(@Param("limit") int limit);
}
