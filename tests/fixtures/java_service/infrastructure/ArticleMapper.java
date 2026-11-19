package io.demo.infrastructure;

@Mapper
public interface ArticleMapper {
  void insert(@Param("article") Article article);
}
